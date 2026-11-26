"""Question 2 (revenue-cycle lead): which denied claims should staff work first today?

Method
------
1. Overturn model. For every appeal whose outcome was known before a cutoff date, fit a logistic
   regression of ``overturned`` on payer, denial-reason category, service line, centred log claim
   charge and claim age at denial (months from submission). Data come from
   ``mart.denial_episodes(cutoff)``, which only sees events and postings dated on or before the
   cutoff, so a later appeal can never change an earlier fit.
2. Held-out check. Score appeals on denials dated on or after the cutoff whose outcome is known
   by the as-of date: Brier score, log loss, AUC, calibration-in-the-large, calibration slope and
   a reliability table, against two baselines (one overall rate; one rate per reason category).
3. Value of working a claim::

       value_i = p_i * R_i - c_i          (0 when the appeal window has closed)
       R_i     = contract_allowed_i * rho_payer   (recoverable amount, net of contractual adjustment)
       c_i     = minutes_i * loaded cost per minute

   ``rho_payer`` is the share of contract allowance the payer actually paid on clean lines before
   the cutoff (nets out patient responsibility and typical underpayment).
4. Today's list. Choose the claims to work under a staff-minutes budget by exact 0/1 knapsack
   (dynamic programming over integer minutes). With equal minutes per claim the knapsack reduces to
   "sort by value and take the top k", which is then provably optimal.
5. Backtest. Replay every business day of the held-out period: each day the policy picks from the
   denials still inside their appeal window, under a daily budget. Compare the knapsack with
   "oldest first" and "largest first" on expected dollars (all held-out denials) and on realised
   dollars (held-out denials that were appealed and whose outcome is known).

All behavioural numbers are estimated from the database. Business parameters (minutes, cost,
staff hours, appeal window) are financial assumptions and are labelled as such.
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import pandas as pd

from .. import FIGURES_DIR, REPO_ROOT, labels

SQL_FILE = REPO_ROOT / "sql" / "models" / "55_work_queue.sql"

VALIDATION_CUTOFF = "2025-07-01"
BUDGET_GRID_HOURS = (0.75, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0, 24.0)
POLICIES = ("MODEL_KNAPSACK", "OLDEST_FIRST", "LARGEST_FIRST")
REASONS = ("ADMINISTRATIVE", "ELIGIBILITY", "CODING", "PRIOR_AUTH", "MEDICAL_NECESSITY", "NON_COVERED")

# Assumptions that are not in config/calibration.yaml. Each one is labelled financial_assumption.
APPEAL_WINDOW = {
    "value": 90,
    "unit": "days from the denial date",
    "label": labels.FINANCIAL_ASSUMPTION,
    "source": ("Assumption. Provider appeal deadlines are set by each payer contract. For context, Medicare's "
               "first-level appeal (redetermination) must be filed within 120 days of receiving the initial "
               "determination (42 CFR 405.942). A single 90-day window is used for every simulated payer."),
}
EFFORT_MULTIPLIER = {
    "value": {"ADMINISTRATIVE": 0.6, "ELIGIBILITY": 0.7, "CODING": 0.8, "PRIOR_AUTH": 1.2,
              "MEDICAL_NECESSITY": 1.6, "NON_COVERED": 1.0},
    "label": labels.FINANCIAL_ASSUMPTION,
    "source": ("Assumption, illustrative and not measured. Relative staff effort per appeal by denial category "
               "(clinical-record appeals take longer than demographic corrections). Multiplies the configured "
               "minutes per appeal. A uniform-effort sensitivity (all multipliers 1) is reported alongside."),
}


# ------------------------------------------------------------------------------------------
# small exact algorithms (unit tested)
# ------------------------------------------------------------------------------------------
def knapsack_01(values: Sequence[float], weights: Sequence[int], capacity: int) -> list[int]:
    """Exact 0/1 knapsack by dynamic programming over integer weights.

    Returns the sorted indices of the chosen items. Items with value <= 0 or weight > capacity are
    never chosen. Runs in O(n * capacity) time. Ties are broken deterministically (the earlier item
    is preferred when including it does not lower the optimum).
    """
    capacity = int(capacity)
    v = np.asarray(values, dtype=float)
    w = np.asarray(weights, dtype=int)
    if np.any(w <= 0):
        raise ValueError("weights must be positive integers")
    cand = [i for i in range(len(v)) if v[i] > 0 and w[i] <= capacity]
    if not cand or capacity <= 0:
        return []
    if int(w[cand].sum()) <= capacity:
        return sorted(cand)
    best = np.zeros(capacity + 1)
    keep = np.zeros((len(cand), capacity + 1), dtype=bool)
    for k, i in enumerate(cand):
        wi, vi = int(w[i]), float(v[i])
        shifted = np.full(capacity + 1, -np.inf)
        shifted[wi:] = best[:capacity + 1 - wi] + vi
        take = shifted > best + 1e-9
        keep[k] = take
        best = np.where(take, shifted, best)
    chosen, cap = [], capacity
    for k in range(len(cand) - 1, -1, -1):
        if keep[k, cap]:
            chosen.append(cand[k])
            cap -= int(w[cand[k]])
    return sorted(chosen)


def first_fit(order: Sequence[int], weights: Sequence[int], capacity: int) -> list[int]:
    """Walk items in ``order`` and take each one that still fits (the rule-based baselines)."""
    out, used = [], 0
    for i in order:
        if used + int(weights[i]) <= capacity:
            out.append(int(i))
            used += int(weights[i])
    return sorted(out)


def classification_metrics(y: np.ndarray, p: np.ndarray) -> dict[str, float]:
    """Brier score, log loss, AUC, calibration-in-the-large and calibration slope."""
    from sklearn.metrics import roc_auc_score
    import statsmodels.api as sm

    y = np.asarray(y, dtype=float)
    p = np.clip(np.asarray(p, dtype=float), 1e-6, 1 - 1e-6)
    out = {
        "n": int(len(y)),
        "observed_rate": float(y.mean()),
        "mean_predicted": float(p.mean()),
        "brier": float(np.mean((p - y) ** 2)),
        "log_loss": float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p))),
        "auc": float(roc_auc_score(y, p)) if 0 < y.sum() < len(y) and np.ptp(p) > 0 else 0.5,
    }
    lp = np.log(p / (1 - p))
    if np.ptp(lp) > 0 and len(y) >= 20:
        fit = sm.GLM(y, sm.add_constant(lp), family=sm.families.Binomial()).fit()
        out["calibration_intercept"] = float(fit.params[0])
        out["calibration_slope"] = float(fit.params[1])
        out["calibration_slope_ci95"] = [float(x) for x in fit.conf_int()[1]]
    return out


def reliability_table(y: np.ndarray, p: np.ndarray, n_bins: int = 10) -> list[dict]:
    """Observed rate by quantile bin of the predicted probability."""
    df = pd.DataFrame({"y": np.asarray(y, float), "p": np.asarray(p, float)})
    df["bin"] = pd.qcut(df["p"].rank(method="first"), q=min(n_bins, len(df)), labels=False)
    g = df.groupby("bin").agg(n=("y", "size"), mean_predicted=("p", "mean"), observed_rate=("y", "mean"))
    g["se"] = np.sqrt(g["observed_rate"] * (1 - g["observed_rate"]) / g["n"])
    return [{k: (int(v) if k == "n" else float(v)) for k, v in r.items()} for r in g.reset_index(drop=True).to_dict("records")]


# ------------------------------------------------------------------------------------------
# data access
# ------------------------------------------------------------------------------------------
def _frame(conn, query: str, params: tuple = ()) -> pd.DataFrame:
    cur = conn.execute(query, params)
    df = pd.DataFrame(cur.fetchall(), columns=[d.name for d in cur.description])
    for c in df.columns:
        if df[c].dtype == object and len(df) and type(df[c].dropna().iloc[0]).__name__ == "Decimal":
            df[c] = df[c].astype(float)
    return df


def denial_episodes(conn, as_of: str | date) -> pd.DataFrame:
    df = _frame(conn, "select * from mart.denial_episodes(%s::date)", (str(as_of),))
    for c in ("submission_date", "denial_date", "next_date", "appeal_date", "outcome_date"):
        df[c] = pd.to_datetime(df[c])
    return df


def payer_paid_ratio(conn, as_of: str | date) -> dict[str, float]:
    df = _frame(conn, "select * from mart.payer_paid_ratio_at(%s::date)", (str(as_of),))
    return {r.payer_id: float(r.ratio) for r in df.itertuples()}


# ------------------------------------------------------------------------------------------
# overturn model
# ------------------------------------------------------------------------------------------
def add_features(df: pd.DataFrame, ln_center: float | None = None) -> tuple[pd.DataFrame, float]:
    out = df.copy()
    ln_amt = np.log(out["claim_total_charge"].astype(float).clip(lower=1.0))
    ln_center = float(ln_amt.mean()) if ln_center is None else ln_center
    out["ln_amount_c"] = ln_amt - ln_center
    out["age_months"] = (out["denial_date"] - out["submission_date"]).dt.days / 30.0
    if "appeal_outcome" in out:
        out["overturned"] = (out["appeal_outcome"] == "OVERTURNED").astype(int)
    return out, ln_center


class OverturnModel:
    """Logistic regression of appeal overturn, with treatment coding (reference = most common level)."""

    CATS = ("payer_id", "reason_category", "service_line_code")

    def __init__(self, train: pd.DataFrame):
        import statsmodels.formula.api as smf
        import statsmodels.api as sm

        tr, self.ln_center = add_features(train)
        self.levels = {c: sorted(tr[c].dropna().unique()) for c in self.CATS}
        self.reference = {c: tr[c].value_counts().sort_index().idxmax() for c in self.CATS}
        terms = [f"C({c}, Treatment(reference='{self.reference[c]}'))" for c in self.CATS]
        self.formula = "overturned ~ " + " + ".join(terms) + " + ln_amount_c + age_months"
        self.result = smf.glm(self.formula, data=tr, family=sm.families.Binomial()).fit()
        self.n_train = int(len(tr))
        self.train_rate = float(tr["overturned"].mean())

    def _prepare(self, df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
        x, _ = add_features(df, self.ln_center)
        n_unseen = 0
        for c in self.CATS:
            unseen = ~x[c].isin(self.levels[c])
            n_unseen += int(unseen.sum())
            x.loc[unseen, c] = self.reference[c]   # unseen level -> reference level (counted, reported)
        return x, n_unseen

    def predict(self, df: pd.DataFrame) -> np.ndarray:
        if len(df) == 0:
            return np.zeros(0)
        x, _ = self._prepare(df)
        return np.asarray(self.result.predict(x), dtype=float)

    def coefficients(self) -> list[dict]:
        r = self.result
        ci = r.conf_int()
        out = []
        for term in r.params.index:
            out.append({"term": _pretty_term(term), "estimate": float(r.params[term]), "std_error": float(r.bse[term]),
                        "ci_low": float(ci.loc[term, 0]), "ci_high": float(ci.loc[term, 1]),
                        "p_value": float(r.pvalues[term]), "odds_ratio": float(math.exp(r.params[term]))})
        return out


def _pretty_term(term: str) -> str:
    # "C(payer_id, Treatment(reference='P01'))[T.P03]" -> "payer_id=P03"
    if term.startswith("C("):
        var = term[2:term.index(",")]
        lev = term[term.index("[T.") + 3:-1]
        return f"{var}={lev}"
    return term


def reason_rate_baseline(train: pd.DataFrame, test: pd.DataFrame) -> np.ndarray:
    """Overturn rate per reason category in the training data (add-one smoothing toward the mean)."""
    y = (train["appeal_outcome"] == "OVERTURNED").astype(float)
    base = float(y.mean())
    g = pd.DataFrame({"r": train["reason_category"], "y": y}).groupby("r")["y"].agg(["sum", "count"])
    rate = (g["sum"] + base) / (g["count"] + 1)
    return test["reason_category"].map(rate).fillna(base).to_numpy(float)


# ------------------------------------------------------------------------------------------
# value and selection
# ------------------------------------------------------------------------------------------
def handling(cfg: dict, uniform: bool = False) -> dict[str, Any]:
    fa = cfg["financial_assumptions"]
    minutes = float(fa["appeal_handling_minutes"]["value"])
    cost = float(fa["appeal_handling_cost_usd"]["value"])
    return {"base_minutes": minutes, "cost_per_minute": cost / minutes,
            "loaded_hourly_cost": 60.0 * cost / minutes,
            "multiplier": {r: 1.0 for r in REASONS} if uniform else dict(EFFORT_MULTIPLIER["value"]),
            "staff_hours_per_day": float(fa["denial_staff_hours_per_day"]["value"])}


def score(df: pd.DataFrame, model: OverturnModel, ratio: dict[str, float], on_date: pd.Timestamp,
          h: dict[str, Any], window_days: int) -> pd.DataFrame:
    """Add p_overturn, recoverable amount, minutes, cost, days left and value for each denial."""
    out = df.copy()
    out["p_overturn"] = model.predict(out)
    mean_ratio = float(np.mean(list(ratio.values()))) if ratio else 1.0
    out["expected_recoverable"] = out["contract_allowed"].astype(float) * out["payer_id"].map(ratio).fillna(mean_ratio)
    mult = out["reason_category"].map(h["multiplier"]).fillna(1.0)
    out["handling_minutes"] = np.maximum(1, np.rint(h["base_minutes"] * mult)).astype(int)
    out["handling_cost"] = out["handling_minutes"] * h["cost_per_minute"]
    out["days_since_denial"] = (on_date - out["denial_date"]).dt.days
    out["days_left_to_appeal"] = window_days - out["days_since_denial"]
    raw = out["p_overturn"] * out["expected_recoverable"] - out["handling_cost"]
    out["expected_value"] = np.where(out["days_left_to_appeal"] >= 0, raw, 0.0)
    return out


def choose(df: pd.DataFrame, policy: str, capacity_minutes: int) -> list[int]:
    """Positions (0..len-1) of the claims a policy works today. Expired claims are never worked."""
    alive = df["days_left_to_appeal"].to_numpy() >= 0
    w = df["handling_minutes"].to_numpy(int)
    if policy == "MODEL_KNAPSACK":
        v = np.where(alive, df["expected_value"].to_numpy(float), 0.0)
        return knapsack_01(v, w, capacity_minutes)
    idx = np.flatnonzero(alive)
    if policy == "OLDEST_FIRST":
        key = df["denial_date"].to_numpy()[idx]
        order = idx[np.lexsort((df["claim_id"].to_numpy()[idx], key))]
    elif policy == "LARGEST_FIRST":
        key = -df["billed"].to_numpy(float)[idx]
        order = idx[np.lexsort((df["claim_id"].to_numpy()[idx], key))]
    else:
        raise ValueError(policy)
    return first_fit(order, w, capacity_minutes)


def replay(pool: pd.DataFrame, policy: str, hours_per_day: float, start: pd.Timestamp, end: pd.Timestamp,
           window_days: int) -> tuple[dict[str, Any], pd.DataFrame]:
    """Work the pool day by day (weekdays from start to end) under a daily minutes budget.

    ``pool`` must already carry handling_minutes, expected_value_raw (p*R - cost before the window
    rule), billed, denial_date and claim_id. Returns totals and the worked rows with work_date.
    """
    cap = int(round(hours_per_day * 60))
    pool = pool.reset_index(drop=True)
    worked = np.zeros(len(pool), dtype=bool)
    work_date = np.full(len(pool), np.datetime64("NaT", "ns"))
    dd = pool["denial_date"].to_numpy()
    for d in pd.bdate_range(start, end):
        dn = np.datetime64(d, "ns")
        age = (dn - dd).astype("timedelta64[D]").astype(int)
        elig = np.flatnonzero((~worked) & (age >= 0) & (age <= window_days))
        if len(elig) == 0:
            continue
        sub = pool.iloc[elig].copy()
        sub["days_left_to_appeal"] = window_days - age[elig]
        sub["expected_value"] = sub["expected_value_raw"]
        pick = choose(sub, policy, cap)
        rows = elig[pick]
        worked[rows] = True
        work_date[rows] = dn
    w = pool[worked].copy()
    w["work_date"] = work_date[worked]
    expired = (~worked) & ((pool["denial_date"] + np.timedelta64(int(window_days), "D")) < np.datetime64(pd.Timestamp(end), "ns")).to_numpy()
    tot = {"n_pool": int(len(pool)), "n_worked": int(worked.sum()),
           "minutes_used": int(w["handling_minutes"].sum()),
           "expected_net": float(w["expected_value_raw"].sum()),
           "n_expired_unworked": int(expired.sum()),
           "expected_value_lost_to_expiry": float(np.clip(pool.loc[expired, "expected_value_raw"], 0, None).sum())}
    if "realised_net" in pool:
        tot["realised_net"] = float(w["realised_net"].sum())
        tot["realised_overturns"] = int(w["overturned"].sum())
    return tot, w


# ------------------------------------------------------------------------------------------
# figures
# ------------------------------------------------------------------------------------------
def _fig_reliability(rel_model: list[dict], rel_base: list[dict], path: Path, cutoff: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(6, 5.2))
    ax.plot([0, 1], [0, 1], color="0.6", lw=1, ls=":", label="perfect calibration")
    for rel, mk, ls, col, name in ((rel_model, "o", "-", "0.1", "logistic model"),
                                   (rel_base, "s", "--", "0.5", "reason-category rate (baseline)")):
        x = [r["mean_predicted"] for r in rel]
        y = [r["observed_rate"] for r in rel]
        e = [1.96 * r["se"] for r in rel]
        ax.errorbar(x, y, yerr=e, marker=mk, ls=ls, color=col, capsize=2, label=name)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.set_xlabel("Predicted probability of overturn (bin mean)")
    ax.set_ylabel("Observed overturn rate (95% binomial interval)")
    ax.set_title(f"Appeal overturn: reliability on held-out appeals\n(denials on or after {cutoff}; simulated data)",
                 fontsize=10)
    ax.legend(loc="upper left", fontsize=8, frameon=False)
    ax.grid(alpha=0.25)
    fig.text(0.01, 0.01, "label: model_estimate on synthetic_record", fontsize=7, color="0.4")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def _fig_backtest(bt: pd.DataFrame, path: Path, cfg_hours: float) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    style = {"MODEL_KNAPSACK": ("0.05", "-", "o"), "OLDEST_FIRST": ("0.45", "--", "s"),
             "LARGEST_FIRST": ("0.65", ":", "^")}
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.4))
    for ax, pool, col, ttl in ((axes[0], "ALL_DENIALS", "expected_net", "Expected net recovery, all held-out denials\n(model probabilities)"),
                               (axes[1], "APPEALED_WITH_OUTCOME", "realised_net", "Realised net recovery, appealed held-out denials\n(actual outcomes)")):
        sub = bt[bt["pool"] == pool]
        for pol in POLICIES:
            s = sub[sub["policy"] == pol].sort_values("hours_per_day")
            c, ls, mk = style[pol]
            ax.plot(s["hours_per_day"], s[col] / 1e3, color=c, ls=ls, marker=mk, label=pol.replace("_", " ").lower())
        ax.set_xscale("log")
        ax.set_xticks(list(BUDGET_GRID_HOURS))
        ax.set_xticklabels([f"{h:g}" for h in BUDGET_GRID_HOURS])
        ax.axvline(cfg_hours, color="0.75", lw=1)
        ax.set_xlabel("Staff hours per business day spent on appeals (log scale)")
        ax.set_ylabel("Net dollars over the held-out period ($ thousand)")
        ax.set_title(ttl, fontsize=10)
        ax.grid(alpha=0.25)
    axes[0].legend(fontsize=8, frameon=False)
    fig.text(0.01, 0.01, "Simulated hospital. Vertical line = configured denial-team hours. "
             "label: model_estimate (handling cost: financial_assumption)", fontsize=7, color="0.4")
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    fig.savefig(path, dpi=150)
    plt.close(fig)


def _fig_coefficients(coefs: list[dict], path: Path, cutoff: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rows = [c for c in coefs if c["term"] != "Intercept"]
    rows = sorted(rows, key=lambda c: c["term"])
    fig, ax = plt.subplots(figsize=(7, 0.32 * len(rows) + 1.4))
    y = np.arange(len(rows))
    ax.errorbar([r["estimate"] for r in rows], y,
                xerr=[[r["estimate"] - r["ci_low"] for r in rows], [r["ci_high"] - r["estimate"] for r in rows]],
                fmt="o", color="0.15", capsize=2)
    ax.axvline(0, color="0.6", lw=1)
    ax.set_yticks(y)
    ax.set_yticklabels([r["term"] for r in rows], fontsize=8)
    ax.set_xlabel("Log-odds of overturn vs reference level (95% CI)")
    ax.set_title(f"Overturn model coefficients (fit on appeals decided before {cutoff})", fontsize=10)
    ax.grid(alpha=0.25, axis="x")
    fig.text(0.01, 0.005, "label: model_estimate on synthetic_record", fontsize=7, color="0.4")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


# ------------------------------------------------------------------------------------------
# run
# ------------------------------------------------------------------------------------------
def _insert(conn, table: str, df: pd.DataFrame) -> None:
    if len(df) == 0:
        return
    cols = list(df.columns)
    rows = [tuple(None if (isinstance(v, float) and math.isnan(v)) else (v.item() if hasattr(v, "item") else v)
                  for v in r) for r in df.itertuples(index=False, name=None)]
    with conn.cursor() as cur:
        cur.executemany(f"insert into {table} ({', '.join(cols)}) values ({', '.join(['%s'] * len(cols))})", rows)


def _reason_text(r: pd.Series) -> str:
    if r["days_left_to_appeal"] < 0:
        return f"Past the {APPEAL_WINDOW['value']}-day appeal window ({-int(r['days_left_to_appeal'])} days late): worth $0."
    head = "Work today" if r["selected"] else ("Not worth working: expected value below handling cost"
                                              if r["expected_value"] <= 0 else "Positive value but outside today's hours")
    return (f"{head}. P(overturn)={r['p_overturn']:.2f} x ${r['expected_recoverable']:,.0f} recoverable "
            f"- ${r['handling_cost']:,.0f} handling = ${r['expected_value']:,.0f}; "
            f"{r['reason_category'].lower().replace('_', ' ')} denial, {int(r['days_left_to_appeal'])} days left to appeal.")


def todays_queue(episodes: pd.DataFrame, model: OverturnModel, ratio: dict, as_of: pd.Timestamp,
                 h: dict, window: int, capacity: int) -> pd.DataFrame:
    """Rank every claim whose latest status on the as-of date is DENIED with no action yet."""
    cand = episodes[episodes["next_status"].isna()].reset_index(drop=True)
    sc = score(cand, model, ratio, as_of, h, window)
    pick = set(choose(sc, "MODEL_KNAPSACK", capacity))
    sc["selected"] = [i in pick for i in range(len(sc))]
    sc["density"] = sc["expected_value"] / sc["handling_minutes"]
    sc = sc.sort_values(["selected", "expected_value", "days_left_to_appeal", "claim_id"],
                        ascending=[False, False, True, True]).reset_index(drop=True)
    sc["queue_rank"] = np.arange(1, len(sc) + 1)
    sc["cumulative_minutes"] = np.where(sc["selected"], sc["handling_minutes"].where(sc["selected"], 0).cumsum(), np.nan)
    sc["reason"] = sc.apply(_reason_text, axis=1)
    return sc


def run(conn, cfg: dict) -> dict:
    conn.execute(SQL_FILE.read_text(encoding="utf-8"))
    as_of = pd.Timestamp(cfg["as_of_date"])
    cutoff = pd.Timestamp(VALIDATION_CUTOFF)
    window = int(APPEAL_WINDOW["value"])
    h = handling(cfg)
    h_uniform = handling(cfg, uniform=True)
    cfg_hours = h["staff_hours_per_day"]

    # ---- 1. fit before the cutoff, evaluate after it
    ep_cut = denial_episodes(conn, cutoff.date())
    train = ep_cut[ep_cut["appeal_outcome"].notna()].reset_index(drop=True)
    ep_all = denial_episodes(conn, as_of.date())
    test = ep_all[(ep_all["denial_date"] >= cutoff) & ep_all["appeal_outcome"].notna()].reset_index(drop=True)
    model_cut = OverturnModel(train)
    y_test = (test["appeal_outcome"] == "OVERTURNED").astype(int).to_numpy()
    p_model = model_cut.predict(test)
    p_base_rate = np.full(len(test), model_cut.train_rate)
    p_base_reason = reason_rate_baseline(train, test)
    met = {"logistic_model": classification_metrics(y_test, p_model),
           "baseline_overall_rate": classification_metrics(y_test, p_base_rate),
           "baseline_reason_rate": classification_metrics(y_test, p_base_reason)}
    rel_model = reliability_table(y_test, p_model)
    rel_base = reliability_table(y_test, p_base_reason)
    _, n_unseen = model_cut._prepare(test)

    # recoverable-amount check on overturned held-out appeals
    ratio_cut = payer_paid_ratio(conn, cutoff.date())
    ov = test[test["appeal_outcome"] == "OVERTURNED"]
    pred_rec = (ov["contract_allowed"] * ov["payer_id"].map(ratio_cut)).sum()
    rec_check = {"n_overturned": int(len(ov)), "predicted_recoverable": float(pred_rec),
                 "realised_paid": float(ov["paid_to_date"].sum()),
                 "realised_over_predicted": float(ov["paid_to_date"].sum() / pred_rec) if pred_rec else None}

    # ---- 2. backtest replay over the held-out period
    pool_a = ep_all[ep_all["denial_date"] >= cutoff].reset_index(drop=True)
    bt_rows, sel_rows, sens_rows = [], [], []
    for pool_name, pool in (("ALL_DENIALS", pool_a),
                            ("APPEALED_WITH_OUTCOME", test)):
        for hh, tag in ((h, "category_effort"), (h_uniform, "uniform_effort")):
            sc = score(pool, model_cut, ratio_cut, as_of, hh, window)
            sc["expected_value_raw"] = sc["p_overturn"] * sc["expected_recoverable"] - sc["handling_cost"]
            if pool_name == "APPEALED_WITH_OUTCOME":
                sc["overturned"] = (sc["appeal_outcome"] == "OVERTURNED").astype(int)
                sc["realised_net"] = sc["overturned"] * sc["paid_to_date"] - sc["handling_cost"]
            for hours in BUDGET_GRID_HOURS:
                for pol in POLICIES:
                    tot, worked = replay(sc, pol, hours, cutoff, as_of, window)
                    row = {"pool": pool_name, "policy": pol, "hours_per_day": hours, **tot}
                    if tag == "category_effort":
                        bt_rows.append(row)
                        sel_rows.append(pd.DataFrame({"pool": pool_name, "policy": pol, "hours_per_day": hours,
                                                      "claim_id": worked["claim_id"], "denial_seq": worked["denial_seq"],
                                                      "work_date": worked["work_date"].dt.date}))
                    else:
                        sens_rows.append(row)
    bt = pd.DataFrame(bt_rows)
    sens = pd.DataFrame(sens_rows)

    # ---- 3. production model and today's list
    resolved = ep_all[ep_all["appeal_outcome"].notna()].reset_index(drop=True)
    model_now = OverturnModel(resolved)
    ratio_now = payer_paid_ratio(conn, as_of.date())
    cap_today = int(round(cfg_hours * 60))
    queue = todays_queue(ep_all, model_now, ratio_now, as_of, h, window, cap_today)
    queue_uniform = todays_queue(ep_all, model_now, ratio_now, as_of, h_uniform, window, cap_today)
    # baselines on today's list, same budget
    q_sorted = queue.sort_values("claim_id").reset_index(drop=True)
    today_policy = {}
    for pol in POLICIES:
        pick = choose(q_sorted, pol, cap_today)
        s = q_sorted.iloc[pick]
        today_policy[pol] = {"n_worked": int(len(s)), "minutes": int(s["handling_minutes"].sum()),
                             "expected_net": float(s["expected_value"].sum())}

    # ---- 4. write marts
    conn.execute("delete from mart.work_queue")
    wq = queue.assign(as_of_date=as_of.date())[[
        "as_of_date", "queue_rank", "selected", "claim_id", "payer_id", "service_line_code", "reason_category",
        "reason_code", "denial_date", "days_since_denial", "days_left_to_appeal", "billed", "contract_allowed",
        "expected_recoverable", "p_overturn", "handling_minutes", "handling_cost", "expected_value",
        "cumulative_minutes", "reason"]].copy()
    wq["denial_date"] = wq["denial_date"].dt.date
    for c in ("expected_recoverable", "handling_cost", "expected_value", "billed", "contract_allowed"):
        wq[c] = wq[c].round(2)
    wq["cumulative_minutes"] = wq["cumulative_minutes"].astype("Int64").astype(object).where(wq["cumulative_minutes"].notna(), None)
    _insert(conn, "mart.work_queue", wq)
    coef_cut = model_cut.coefficients()
    coef_now = model_now.coefficients()
    _insert(conn, "mart.work_queue_coef", pd.DataFrame(
        [{"fit_cutoff": cutoff.date(), **c} for c in coef_cut] + [{"fit_cutoff": as_of.date(), **c} for c in coef_now]))
    _insert(conn, "mart.work_queue_backtest", bt[["pool", "policy", "hours_per_day", "n_worked", "minutes_used",
                                                  "expected_net", "n_expired_unworked"]].assign(
        realised_net=bt.get("realised_net")).round(2))
    if sel_rows:
        _insert(conn, "mart.work_queue_backtest_selection", pd.concat(sel_rows, ignore_index=True))
    labels.label_table(conn, "mart.work_queue", labels.MODEL_ESTIMATE,
                       "Today's ranked denial work list: logistic overturn probability x recoverable amount minus "
                       "handling cost (financial_assumption), exact 0/1 knapsack under staff hours. Simulated data.")
    labels.label_table(conn, "mart.work_queue_coef", labels.MODEL_ESTIMATE,
                       "Logistic regression coefficients of appeal overturn, fit before the validation cutoff and at the as-of date.")
    labels.label_table(conn, "mart.work_queue_backtest", labels.MODEL_ESTIMATE,
                       "Daily replay of the held-out period: knapsack vs oldest-first vs largest-first under staff-hour budgets.")
    labels.label_table(conn, "mart.work_queue_backtest_selection", labels.MODEL_ESTIMATE,
                       "Claims each policy worked in the backtest, with the work date.")

    # ---- 5. figures
    FIGURES_DIR.mkdir(parents=True, exist_ok=True)
    figs = {"reliability": FIGURES_DIR / "prioritize_reliability.png",
            "backtest": FIGURES_DIR / "prioritize_backtest.png",
            "coefficients": FIGURES_DIR / "prioritize_coefficients.png"}
    _fig_reliability(rel_model, rel_base, figs["reliability"], VALIDATION_CUTOFF)
    _fig_backtest(bt, figs["backtest"], cfg_hours)
    _fig_coefficients(coef_cut, figs["coefficients"], VALIDATION_CUTOFF)

    # ---- 6. result dict
    ME, FA, SR = labels.MODEL_ESTIMATE, labels.FINANCIAL_ASSUMPTION, labels.SYNTHETIC_RECORD
    st = labels.stamp

    def bt_table(df: pd.DataFrame) -> list[dict]:
        return [{k: (round(v, 2) if isinstance(v, float) else v) for k, v in r.items()} for r in df.to_dict("records")]

    def lift(df: pd.DataFrame, pool: str, col: str) -> list[dict]:
        out = []
        for hours in BUDGET_GRID_HOURS:
            s = df[(df["pool"] == pool) & (df["hours_per_day"] == hours)].set_index("policy")[col]
            out.append({"hours_per_day": hours, "model_knapsack": round(float(s["MODEL_KNAPSACK"]), 2),
                        "oldest_first": round(float(s["OLDEST_FIRST"]), 2), "largest_first": round(float(s["LARGEST_FIRST"]), 2),
                        "knapsack_minus_oldest": round(float(s["MODEL_KNAPSACK"] - s["OLDEST_FIRST"]), 2),
                        "knapsack_minus_largest": round(float(s["MODEL_KNAPSACK"] - s["LARGEST_FIRST"]), 2)})
        return out

    daily_inflow = len(pool_a) / max(1, len(pd.bdate_range(cutoff, as_of)))
    sel = queue[queue["selected"]]
    return {
        "name": "prioritize",
        "question": "Q2 Revenue-cycle lead: which denied claims should staff work first today?",
        "label": ME,
        "note": ("Simulated data for a fictional hospital. Results show whether the method recovers the generator's "
                 "planted behaviour and whether the decision rule behaves correctly; they are not findings about real payers."),
        "seed": cfg["seed"],
        "as_of_date": str(as_of.date()),
        "validation_cutoff": VALIDATION_CUTOFF,
        "inputs": {
            "tables": ["core.fact_claim_event", "core.fact_claim_line", "core.fact_remit_line",
                       "recon.claim_remit_match", "recon.claim_line_balance"],
            "sql": "sql/models/55_work_queue.sql",
            "calibration_sha256": cfg.get("calibration_sha256"),
        },
        "assumptions": {
            "appeal_window_days": APPEAL_WINDOW,
            "handling_minutes_base": st(h["base_minutes"], FA, source="config/calibration.yaml financial_assumptions.appeal_handling_minutes"),
            "handling_cost_per_appeal_usd": st(cfg["financial_assumptions"]["appeal_handling_cost_usd"]["value"], FA,
                                               source="config/calibration.yaml financial_assumptions.appeal_handling_cost_usd"),
            "loaded_hourly_cost_usd": st(round(h["loaded_hourly_cost"], 2), FA,
                                         note="implied: cost per appeal / minutes per appeal x 60"),
            "effort_multiplier_by_reason": EFFORT_MULTIPLIER,
            "staff_hours_per_day": st(cfg_hours, FA, source="config/calibration.yaml financial_assumptions.denial_staff_hours_per_day"),
            "business_days": "weekdays (public holidays ignored in the replay)",
            "timing_independence": ("The backtest assumes an appeal's outcome does not depend on the day it is filed "
                                    "inside the window, so a claim worked on another day than in reality keeps its realised outcome."),
        },
        "data": {
            "train_appeals": st(model_cut.n_train, SR, note=f"appeals decided on or before {VALIDATION_CUTOFF}"),
            "train_overturn_rate": st(round(model_cut.train_rate, 4), SR),
            "test_appeals": st(int(len(test)), SR, note="appeals on denials dated on or after the cutoff, outcome known by the as-of date"),
            "test_overturn_rate": st(round(float(y_test.mean()), 4), SR),
            "test_levels_unseen_in_training": st(n_unseen, SR),
            "held_out_denials": st(int(len(pool_a)), SR),
            "held_out_denials_per_business_day": st(round(daily_inflow, 2), SR),
            "appeals_resolved_by_as_of": st(int(len(resolved)), SR),
        },
        "model": {
            "formula": model_cut.formula,
            "reference_levels": model_cut.reference,
            "ln_amount_center": round(model_cut.ln_center, 6),
            "coefficients_fit_at_cutoff": [{**c, "label": ME} for c in coef_cut],
            "coefficients_fit_at_as_of": [{**c, "label": ME} for c in coef_now],
            "deviance": round(float(model_cut.result.deviance), 3),
            "null_deviance": round(float(model_cut.result.null_deviance), 3),
            "aic": round(float(model_cut.result.aic), 3),
        },
        "validation": {
            "held_out_metrics": {k: {kk: (round(vv, 4) if isinstance(vv, float) else vv) for kk, vv in v.items()}
                                 for k, v in met.items()},
            "metrics_label": ME,
            "reliability_model": rel_model,
            "reliability_reason_baseline": rel_base,
            "recoverable_amount_check": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in rec_check.items()},
            "payer_paid_ratio_at_cutoff": {k: round(v, 4) for k, v in ratio_cut.items()},
        },
        "backtest": {
            "description": ("Every weekday from the cutoff to the as-of date each policy picks claims inside the appeal "
                            "window under the daily budget; a worked claim leaves the pool. ALL_DENIALS scores choices "
                            "with the model's expected value (circular for the knapsack, which maximises it by "
                            "construction); APPEALED_WITH_OUTCOME scores them with the realised outcome and paid amount."),
            "expected_net_all_denials": lift(bt, "ALL_DENIALS", "expected_net"),
            "realised_net_appealed_with_outcome": lift(bt, "APPEALED_WITH_OUTCOME", "realised_net"),
            "uniform_effort_sensitivity_expected_net": lift(sens, "ALL_DENIALS", "expected_net"),
            "uniform_effort_sensitivity_realised_net": lift(sens, "APPEALED_WITH_OUTCOME", "realised_net"),
            "rows": bt_table(bt),
            "label": ME,
        },
        "todays_queue": {
            "as_of_date": str(as_of.date()),
            "candidates": st(int(len(queue)), SR, note="claims whose latest status is DENIED with no action yet"),
            "past_appeal_window": st(int((queue["days_left_to_appeal"] < 0).sum()), SR),
            "non_positive_value": st(int(((queue["days_left_to_appeal"] >= 0) & (queue["expected_value"] <= 0)).sum()), ME),
            "budget_minutes": st(cap_today, FA),
            "selected": st(int(len(sel)), ME),
            "selected_minutes": st(int(sel["handling_minutes"].sum()), ME),
            "selected_expected_net": st(round(float(sel["expected_value"].sum()), 2), ME),
            "policies_same_budget": {k: {kk: (round(vv, 2) if isinstance(vv, float) else vv) for kk, vv in v.items()}
                                     for k, v in today_policy.items()},
            "uniform_effort_selected": st(int(queue_uniform["selected"].sum()), ME),
            "uniform_effort_expected_net": st(round(float(queue_uniform.loc[queue_uniform["selected"], "expected_value"].sum()), 2), ME),
            "top10": [{"rank": int(r.queue_rank), "claim_id": r.claim_id, "payer_id": r.payer_id,
                       "reason_category": r.reason_category, "p_overturn": round(float(r.p_overturn), 3),
                       "expected_value": round(float(r.expected_value), 2), "reason": r.reason}
                      for r in queue.head(10).itertuples()],
        },
        "marts": ["mart.work_queue", "mart.work_queue_coef", "mart.work_queue_backtest", "mart.work_queue_backtest_selection"],
        "figures": [str(p.relative_to(REPO_ROOT)) for p in figs.values()],
    }
