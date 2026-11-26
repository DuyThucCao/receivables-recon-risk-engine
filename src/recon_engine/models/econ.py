"""Question 7 (consulting economist): which payers underpay against contract, by how much, and
why would they?

a. Underpayment (``sql/models/63``): every priced claim line is compared with its contract amount
   in allowed terms. The shortfall at first pricing is separated from patient responsibility
   (part of allowed), denials (not priced), partial payments still pending (timing) and later
   repricing. Per payer: share of claims underpaid, shortfall dollars and shortfall rate, each
   with a 95% bootstrap interval that resamples whole claims (lines of one claim share one
   pricing decision, so they are not independent).
b. Value of float (``sql/models/64``): amount x SOFR x days / 365, in total and beyond an assumed
   prompt-pay window.
c. Incentives (this module): a one-period model of the payer's choice and the hospital's
   break-even appeal rule, computed per payer from the data, plus a repeated-game reading.
   Economics as reasoning, not causal estimates.

All payers and amounts are simulated. Only SOFR is a real public series. Nothing here is a
finding about real payers.
"""
from __future__ import annotations

import csv
from datetime import date
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

from .. import DATA_DIR, FIGURES_DIR, SQL_DIR
from .. import labels as L

NAME = "econ"
SQL_FILES = ["63_econ_underpayment.sql", "64_econ_float.sql", "65_econ_appeals.sql"]
SOFR_CSV = DATA_DIR / "public" / "fred_sofr" / "SOFR.csv"

# Assumptions of this module (each is stamped financial_assumption in the output).
ASSUMPTIONS = {
    "shortfall_tolerance_usd": (0.01, "A line counts as priced below contract if allowed is at least one cent below the "
                                      "contract amount. Contract arithmetic in this data set is exact to the cent; real "
                                      "data would need a wider tolerance for rounding."),
    "prompt_pay_days": (30, "Days from submission after which a payment counts as late. A round assumption, not a "
                            "statute; prompt-pay windows differ by state, payer type and claim type."),
    "bootstrap_reps": (2000, "Bootstrap replicates per payer."),
    "p_win_dispute_alt": (0.90, "Sensitivity: assumed win rate for an underpayment dispute, where the contract rate is "
                                "documented (likely higher than the denial-appeal win rate used as the base case)."),
    "prompt_pay_interest_rate": (0.12, "Illustrative contract clause: simple interest per year owed on late or short "
                                       "payments. Assumed, not taken from any statute or contract."),
    "renewal_discount_factor": (0.90, "Weight the payer puts on next contract period relative to this one "
                                      "(probability of renewal x time discount). Illustrative."),
}

CONFIG_KEYS = {  # module key -> key under financial_assumptions in config/calibration.yaml
    "shortfall_tolerance_usd": "underpayment_shortfall_tolerance_usd",
    "prompt_pay_days": "prompt_pay_window_days",
    "p_win_dispute_alt": "dispute_win_rate",
    "prompt_pay_interest_rate": "prompt_pay_interest_rate_annual",
    "renewal_discount_factor": "contract_renewal_discount_factor",
}


# ------------------------------------------------------------------------------------------
# pure functions (unit-tested)
# ------------------------------------------------------------------------------------------
def float_value(amount, annual_rate, days):
    """Simple-interest value of holding ``amount`` for ``days`` at ``annual_rate``: A x r x d / 365."""
    return np.asarray(amount, dtype=float) * np.asarray(annual_rate, dtype=float) * np.asarray(days, dtype=float) / 365.0


def appeal_threshold(appeal_cost: float, p_win: float) -> float:
    """Hospital break-even: appeal a shortfall s when s x P(win) > cost, i.e. when s > cost / P(win)."""
    if not 0 < p_win <= 1:
        raise ValueError("p_win must be in (0, 1]")
    return appeal_cost / p_win


def expected_net_recovery(shortfall, p_win: float, appeal_cost: float):
    """Expected value of appealing one shortfall: P(win) x s - cost."""
    return p_win * np.asarray(shortfall, dtype=float) - appeal_cost


def payer_expected_gain(shortfall, q: float, p_win: float, penalty: float = 0.0, payer_admin_cost: float = 0.0):
    """Payer's expected gain from underpaying a claim by s.

    With probability q the hospital appeals; the hospital then wins with probability p_win and the
    payer repays s plus a penalty (penalty x s); the payer spends payer_admin_cost on every appeal.
        gain = s - q * (p_win * (1 + penalty) * s + payer_admin_cost)
    """
    s = np.asarray(shortfall, dtype=float)
    return s - q * (p_win * (1.0 + penalty) * s + payer_admin_cost)


def deterrence_penalty(q: float, p_win: float) -> float:
    """Smallest penalty (as a multiple of s) that makes underpaying unprofitable when the payer's
    own appeal-handling cost is ignored: s - q*w*(1+pi)*s <= 0  <=>  pi >= 1/(q*w) - 1."""
    if q * p_win <= 0:
        return float("inf")
    return 1.0 / (q * p_win) - 1.0


def repeated_game_min_loss(one_period_gain: float, delta: float) -> float:
    """Grim-trigger reading: cheating gains G now; if detected the hospital retaliates (tougher
    terms, exit) and the payer loses L every later period. Cheating does not pay when
    G <= delta / (1 - delta) * L, so the smallest per-period loss that deters is G (1 - delta) / delta."""
    if not 0 < delta < 1:
        raise ValueError("delta must be in (0, 1)")
    return one_period_gain * (1.0 - delta) / delta


def bootstrap_sums(X: np.ndarray, reps: int, seed: int, chunk: int = 200) -> np.ndarray:
    """Cluster bootstrap of column sums.

    ``X`` has one row per cluster (claim) and one column per additive quantity (for example
    lines, underpaid lines, shortfall dollars, expected dollars). Each replicate draws as many
    clusters as there are, with replacement, and returns the column sums; ratios of these sums
    give the bootstrap distribution of rates. Deterministic for a given seed.
    """
    X = np.asarray(X, dtype=float)
    n = X.shape[0]
    rng = np.random.default_rng(seed)
    out = np.empty((reps, X.shape[1]))
    done = 0
    while done < reps:
        b = min(chunk, reps - done)
        idx = rng.integers(0, n, size=(b, n))
        out[done:done + b] = X[idx].sum(axis=1)
        done += b
    return out


def percentile_ci(draws: np.ndarray, level: float = 0.95) -> tuple[float, float]:
    a = (1 - level) / 2
    lo, hi = np.nanquantile(draws, [a, 1 - a])
    return float(lo), float(hi)


def wilson_ci(k: int, n: int, z: float = 1.959964) -> tuple[float, float]:
    if n == 0:
        return float("nan"), float("nan")
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return float(c - h), float(c + h)


# ------------------------------------------------------------------------------------------
# database helpers
# ------------------------------------------------------------------------------------------
def _frame(conn, sql: str, params: tuple = ()) -> pd.DataFrame:
    cur = conn.execute(sql, params)
    return pd.DataFrame(cur.fetchall(), columns=[d.name for d in cur.description])


def _write_frame(conn, df: pd.DataFrame, table: str) -> None:
    """Replace ``table`` with the contents of ``df`` (numeric -> numeric, text -> text)."""
    def pgtype(s: pd.Series) -> str:
        if pd.api.types.is_bool_dtype(s):
            return "boolean"
        if pd.api.types.is_integer_dtype(s):
            return "bigint"
        if pd.api.types.is_numeric_dtype(s):
            return "numeric"
        return "text"
    cols = ", ".join(f'"{c}" {pgtype(df[c])}' for c in df.columns)
    conn.execute(f"drop table if exists {table}")
    conn.execute(f"create table {table} ({cols})")
    rows = [tuple(None if (isinstance(v, float) and np.isnan(v)) else (v.item() if hasattr(v, "item") else v)
                  for v in r) for r in df.itertuples(index=False, name=None)]
    with conn.cursor() as cur:
        cur.executemany(f"insert into {table} values ({', '.join(['%s'] * len(df.columns))})", rows)


def load_sofr_monthly(conn, path: Path = SOFR_CSV, fallback_rate: float | None = None) -> dict:
    """mart.econ_sofr_month from the FRED SOFR file (daily, percent). Falls back to one constant
    rate (cfg discount rate) for every month if the file is missing."""
    conn.execute("drop table if exists mart.econ_sofr_month")
    conn.execute("create table mart.econ_sofr_month (month date primary key, sofr_pct numeric, n_obs integer)")
    if path.exists():
        vals: dict[date, list[float]] = {}
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                v = row.get("SOFR", "").strip()
                if not v or v == ".":
                    continue
                d = date.fromisoformat(row["observation_date"])
                vals.setdefault(date(d.year, d.month, 1), []).append(float(v))
        rows = [(m, round(sum(v) / len(v), 4), len(v)) for m, v in sorted(vals.items())]
        info = {"source": "FRED SOFR daily series, monthly mean", "label": L.OBSERVED_PUBLIC, "path": str(path.name)}
    else:
        rate = 100 * float(fallback_rate or 0.04)
        rows = [(date(y, m, 1), rate, 0) for y in range(2023, 2027) for m in range(1, 13)]
        info = {"source": "constant discount rate from cfg (SOFR file missing)", "label": L.FINANCIAL_ASSUMPTION}
    with conn.cursor() as cur:
        cur.executemany("insert into mart.econ_sofr_month values (%s, %s, %s)", rows)
    return info


# ------------------------------------------------------------------------------------------
def run(conn, cfg: dict) -> dict:
    as_of = date.fromisoformat(str(cfg["as_of_date"]))
    seed = int(cfg.get("seed", 0))
    A = {k: v[0] for k, v in ASSUMPTIONS.items()}
    fa = cfg.get("financial_assumptions", {})
    # config/calibration.yaml is the source of truth when it carries the assumption (R-DE-4);
    # the module values above stay as a labelled fallback.
    for module_key, config_key in CONFIG_KEYS.items():
        if config_key in fa:
            A[module_key] = type(A[module_key])(fa[config_key]["value"])
    appeal_cost = float(fa.get("appeal_handling_cost_usd", {}).get("value", 45.0))
    appeal_cost_label = fa.get("appeal_handling_cost_usd", {}).get("label", L.FINANCIAL_ASSUMPTION)
    years = (date.fromisoformat(str(cfg["service_end"])) - date.fromisoformat(str(cfg["service_start"]))).days / 365.25

    conn.execute("drop table if exists mart.econ_param")
    conn.execute("create table mart.econ_param as select %s::date as as_of, %s::numeric as shortfall_tolerance, "
                 "%s::integer as prompt_pay_days", (as_of, A["shortfall_tolerance_usd"], A["prompt_pay_days"]))
    sofr_info = load_sofr_monthly(conn, fallback_rate=cfg.get("discount_rate", {}).get("value"))
    for f in SQL_FILES:
        conn.execute((SQL_DIR / "models" / f).read_text(encoding="utf-8"))

    under = underpayment_summary(conn, A["bootstrap_reps"], seed)
    appeals = _frame(conn, "select * from mart.econ_appeal_outcome order by payer_id")
    flt = float_summary(conn)
    incent = incentive_table(conn, under, appeals, flt, appeal_cost, A, years)

    _write_frame(conn, under, "mart.underpayment")
    _write_frame(conn, flt, "mart.float_value")
    _write_frame(conn, incent, "mart.underpayment_incentive")
    tables = {
        "mart.underpayment_line": (L.SYNTHETIC_RECORD, "Priced claim lines: contract vs first and net allowed, gap reason."),
        "mart.underpayment_claim": (L.SYNTHETIC_RECORD, "Claim-level shortfall at first pricing (the unit of an appeal)."),
        "mart.underpayment": (L.MODEL_ESTIMATE, "Per payer: underpaid-claim share, shortfall rate and dollars with 95% claim-clustered bootstrap intervals."),
        "mart.underpayment_incentive": (L.MODEL_ESTIMATE, "Per payer: break-even appeal threshold, recoverable dollars above it, payer-incentive numbers."),
        "mart.float_value_line": (L.MODEL_ESTIMATE, "Paid lines: days to pay, days late, value of float at monthly SOFR."),
        "mart.float_value": (L.MODEL_ESTIMATE, "Per payer: value of float in total and beyond the prompt-pay window."),
        "mart.float_value_open": (L.MODEL_ESTIMATE, "Per payer: float still accruing on open lines past the window (censored)."),
        "mart.econ_appeal_outcome": (L.SYNTHETIC_RECORD, "Per payer: denial appeals decided and won (stand-in for P(win))."),
        "mart.econ_sofr_month": (sofr_info["label"], "Monthly average SOFR used as the opportunity cost of cash."),
    }
    for t, (lab, note) in tables.items():
        L.label_table(conn, t, lab, note)

    out = {
        "name": NAME,
        "question": "Q7 Consulting economist: which payers underpay against contract, by how much, and why would they?",
        "label_note": "Simulated payers and claims for a fictional hospital; SOFR is the only real series. Results show "
                      "parameter recovery and decision logic, not findings about real payers.",
        "seed": seed,
        "cutoff": {"as_of_date": as_of.isoformat(), "service_start": str(cfg["service_start"]),
                   "service_end": str(cfg["service_end"]), "years_of_service_dates": round(years, 3)},
        "inputs": {"tables": ["recon.claim_line_balance", "recon.claim_line_status", "recon.claim_remit_match",
                              "core.fact_remit_line", "core.fact_claim_event", "core.fact_claim_line",
                              "core.dim_contract_rate (through recon.claim_line_balance.expected_allowed)"],
                   "sql": [f"sql/models/{f}" for f in SQL_FILES], "sofr": sofr_info},
        "assumptions": {k: L.stamp(A[k], L.FINANCIAL_ASSUMPTION, note=v[1] + (" Value from config/calibration.yaml." if CONFIG_KEYS.get(k) in fa else "")) for k, v in ASSUMPTIONS.items()},
        "appeal_cost_usd": L.stamp(appeal_cost, appeal_cost_label, note="cfg financial_assumptions.appeal_handling_cost_usd"),
        "gap_reasons": gap_reasons(conn),
        "underpayment_by_payer": _records(under, L.MODEL_ESTIMATE),
        "appeal_outcomes": _records(appeals, L.SYNTHETIC_RECORD),
        "float_by_payer": _records(flt, L.MODEL_ESTIMATE),
        "float_open_by_payer": _records(_frame(conn, "select * from mart.float_value_open"), L.MODEL_ESTIMATE),
        "incentives_by_payer": _records(incent, L.MODEL_ESTIMATE),
        "headline": headline(under, flt, incent),
        "figures": make_figures(under, flt, incent),
        "marts": {t: v[0] for t, v in tables.items()},
    }
    return out


def _clean(v: Any) -> Any:
    from decimal import Decimal
    if v is None:
        return None
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (bool, np.bool_)):
        return bool(v)
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, (float, np.floating)):
        return None if np.isnan(v) else float(v)
    return v


def _records(df: pd.DataFrame, label: str) -> dict:
    return {"label": label, "rows": [{k: _clean(v) for k, v in r.items()} for r in df.to_dict(orient="records")]}


# ------------------------------------------------------------------------------------------
def underpayment_summary(conn, reps: int, seed: int) -> pd.DataFrame:
    """Per payer point estimates and claim-clustered bootstrap intervals."""
    claims = _frame(conn, """select payer_id, claim_id, n_lines, expected_allowed::float8 as expected,
                                    pricing_shortfall::float8 as shortfall, shortfall_fraction::float8 as frac,
                                    is_underpaid, post_payment_reduction::float8 as post_red,
                                    post_payment_increase::float8 as post_inc, pending_partial::float8 as pending,
                                    patient_resp::float8 as patient_resp
                             from mart.underpayment_claim order by payer_id, claim_id""")
    rows = []
    for k, (payer, g) in enumerate(claims.groupby("payer_id", sort=True)):
        u = g.is_underpaid.astype(float).to_numpy()
        X = np.column_stack([np.ones(len(g)), u, g.shortfall.to_numpy(), g.expected.to_numpy(),
                             g.frac.fillna(0).to_numpy() * u])
        draws = bootstrap_sums(X, reps, seed + 1000 * (k + 1))
        inc = draws[:, 1] / draws[:, 0]
        rate = draws[:, 2] / draws[:, 3]
        tot = draws[:, 2]
        mfrac = np.where(draws[:, 1] > 0, draws[:, 4] / np.maximum(draws[:, 1], 1), np.nan)
        s = X.sum(axis=0)
        rows.append({
            "payer_id": payer, "n_priced_claims": int(len(g)), "n_priced_lines": int(g.n_lines.sum()),
            "n_underpaid_claims": int(u.sum()),
            "expected_allowed": round(s[3], 2), "shortfall_usd": round(s[2], 2),
            "shortfall_usd_lo": round(percentile_ci(tot)[0], 2), "shortfall_usd_hi": round(percentile_ci(tot)[1], 2),
            "underpaid_claim_share": round(s[1] / s[0], 5),
            "underpaid_claim_share_lo": round(percentile_ci(inc)[0], 5), "underpaid_claim_share_hi": round(percentile_ci(inc)[1], 5),
            "shortfall_rate": round(s[2] / s[3], 6),
            "shortfall_rate_lo": round(percentile_ci(rate)[0], 6), "shortfall_rate_hi": round(percentile_ci(rate)[1], 6),
            "mean_shortfall_fraction": round(s[4] / s[1], 5) if s[1] else np.nan,
            "mean_shortfall_fraction_lo": round(percentile_ci(mfrac)[0], 5) if s[1] else np.nan,
            "mean_shortfall_fraction_hi": round(percentile_ci(mfrac)[1], 5) if s[1] else np.nan,
            "post_payment_reduction_usd": round(g.post_red.sum(), 2),
            "post_payment_increase_usd": round(g.post_inc.sum(), 2),
            "pending_partial_usd": round(g.pending.sum(), 2),
            "patient_resp_usd": round(g.patient_resp.sum(), 2),
            "bootstrap_reps": reps, "bootstrap_seed": seed + 1000 * (k + 1),
        })
    return pd.DataFrame(rows)


def gap_reasons(conn) -> dict:
    df = _frame(conn, """select payer_id, gap_reason, count(*) as n_lines,
                                sum(expected_allowed - net_allowed)::float8 as gap_vs_contract_usd,
                                sum(pricing_shortfall)::float8 as pricing_shortfall_usd,
                                sum(post_payment_reduction - post_payment_increase)::float8 as net_post_payment_change_usd,
                                sum(patient_resp)::float8 as patient_resp_usd
                         from mart.underpayment_line group by 1, 2 order by 1, 2""")
    return _records(df, L.SYNTHETIC_RECORD)


def float_summary(conn) -> pd.DataFrame:
    df = _frame(conn, """
        select payer_id, count(*) as n_paid_lines, sum(paid)::float8 as paid_usd,
               percentile_cont(0.5) within group (order by days_to_pay) filter (where path = 'FIRST_PASS') as median_days_first_pass,
               avg(days_to_pay) filter (where path = 'FIRST_PASS')::float8 as mean_days_first_pass,
               percentile_cont(0.5) within group (order by days_to_pay) filter (where path = 'PAID_AFTER_DENIAL') as median_days_after_denial,
               avg((days_late > 0)::int)::float8 as share_lines_late,
               (sum(paid * days_late) / nullif(sum(paid), 0))::float8 as paid_weighted_days_late,
               sum(float_total)::float8 as float_total_usd,
               sum(float_late)::float8 as float_late_usd,
               sum(float_late) filter (where path = 'FIRST_PASS')::float8 as float_late_first_pass_usd,
               sum(float_late) filter (where path = 'PAID_AFTER_DENIAL')::float8 as float_late_after_denial_usd,
               (sum(paid * rate) / nullif(sum(paid), 0))::float8 as avg_rate
        from mart.float_value_line group by payer_id order by payer_id""")
    op = _frame(conn, "select payer_id, float_accruing::float8 as float_accruing_open_usd from mart.float_value_open")
    df = df.merge(op, on="payer_id", how="left")
    df["float_late_per_100k_paid"] = 1e5 * df.float_late_usd / df.paid_usd
    return df.round(4)


def incentive_table(conn, under: pd.DataFrame, appeals: pd.DataFrame, flt: pd.DataFrame, appeal_cost: float,
                    A: dict, years: float) -> pd.DataFrame:
    claims = _frame(conn, """select payer_id, pricing_shortfall::float8 as s from mart.underpayment_claim
                             where is_underpaid""")
    rows = []
    for _, a in appeals.iterrows():
        payer = a.payer_id
        decided = int(a.n_won + a.n_lost)
        w = float(a.win_rate) if a.win_rate is not None else float("nan")
        w_lo, w_hi = wilson_ci(int(a.n_won), decided)
        s = claims.loc[claims.payer_id == payer, "s"].to_numpy()
        thr = appeal_threshold(appeal_cost, w)
        above = s > thr
        thr_alt = appeal_threshold(appeal_cost, A["p_win_dispute_alt"])
        above_alt = s > thr_alt
        u = under.loc[under.payer_id == payer].iloc[0]
        f = flt.loc[flt.payer_id == payer].iloc[0]
        retained_threshold = s[~above].sum() + ((1 - w) * s[above]).sum()
        g_annual = (u.shortfall_usd + f.float_late_usd) / years
        rows.append({
            "payer_id": payer,
            "p_win": round(w, 4), "p_win_lo": round(w_lo, 4), "p_win_hi": round(w_hi, 4), "n_appeals_decided": decided,
            "appeal_cost_usd": appeal_cost,
            "threshold_usd": round(thr, 2),
            "n_underpaid_claims": int(len(s)),
            "median_claim_shortfall_usd": round(float(np.median(s)), 2) if len(s) else np.nan,
            "n_above_threshold": int(above.sum()),
            "shortfall_above_threshold_usd": round(float(s[above].sum()), 2),
            "shortfall_below_threshold_usd": round(float(s[~above].sum()), 2),
            "share_dollars_above_threshold": round(float(s[above].sum() / s.sum()), 4) if s.sum() else np.nan,
            "expected_net_recovery_usd": round(float(expected_net_recovery(s[above], w, appeal_cost).sum()), 2),
            "threshold_alt_usd": round(thr_alt, 2),
            "n_above_threshold_alt": int(above_alt.sum()),
            "expected_net_recovery_alt_usd": round(float(expected_net_recovery(s[above_alt], A["p_win_dispute_alt"], appeal_cost).sum()), 2),
            "payer_retains_status_quo_usd": round(float(s.sum()), 2),
            "payer_retains_under_threshold_policy_usd": round(float(retained_threshold), 2),
            "deterrence_penalty_multiple": round(deterrence_penalty(1.0, w), 3),
            "interest_clause_as_share_of_s_90d": round(A["prompt_pay_interest_rate"] * 90 / 365, 4),
            "payer_float_gain_per_dollar_30d_late": round(float(float_value(1.0, f.avg_rate, 30)), 5),
            "payer_cost_per_dollar_30d_late_with_clause": round(float(float_value(1.0, f.avg_rate - A["prompt_pay_interest_rate"], 30)), 5),
            "annual_gain_shortfall_plus_late_float_usd": round(float(g_annual), 2),
            "renewal_min_annual_loss_to_deter_usd": round(repeated_game_min_loss(float(g_annual), A["renewal_discount_factor"]), 2),
        })
    return pd.DataFrame(rows)


def headline(under: pd.DataFrame, flt: pd.DataFrame, incent: pd.DataFrame) -> dict:
    u = under.set_index("payer_id")
    i = incent.set_index("payer_id")
    top = u.shortfall_rate.sort_values(ascending=False)
    M = L.MODEL_ESTIMATE
    return {
        "total_pricing_shortfall_usd": L.stamp(round(float(u.shortfall_usd.sum()), 2), M),
        "payer_highest_shortfall_rate": L.stamp(top.index[0], M),
        "highest_shortfall_rate": L.stamp(float(top.iloc[0]), M),
        "highest_shortfall_rate_ci": L.stamp([float(u.loc[top.index[0], "shortfall_rate_lo"]),
                                              float(u.loc[top.index[0], "shortfall_rate_hi"])], M),
        "total_float_late_usd": L.stamp(round(float(flt.float_late_usd.sum()), 2), M),
        "total_float_total_usd": L.stamp(round(float(flt.float_total_usd.sum()), 2), M),
        "total_expected_net_recovery_usd": L.stamp(round(float(i.expected_net_recovery_usd.sum()), 2), M),
        "total_shortfall_above_threshold_usd": L.stamp(round(float(i.shortfall_above_threshold_usd.sum()), 2), M),
        "payers_ranked_by_shortfall_rate": L.stamp(list(top.index), M),
    }


# ------------------------------------------------------------------------------------------
def make_figures(under: pd.DataFrame, flt: pd.DataFrame, incent: pd.DataFrame, figures_dir: Path = FIGURES_DIR) -> list[str]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figures_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    u = under.sort_values("payer_id")
    x = np.arange(len(u))

    fig, axes = plt.subplots(1, 2, figsize=(10, 4))
    ax = axes[0]
    r = 100 * u.shortfall_rate.astype(float)
    err = [r - 100 * u.shortfall_rate_lo.astype(float), 100 * u.shortfall_rate_hi.astype(float) - r]
    ax.bar(x, r, color="#7f7f7f", edgecolor="black", yerr=err, capsize=4)
    ax.set_xticks(x, u.payer_id)
    ax.set_ylabel("Shortfall at first pricing, % of contract allowed")
    ax.set_title("Shortfall rate by payer (95% claim bootstrap)", fontsize=10)
    ax = axes[1]
    d = u.shortfall_usd.astype(float) / 1e3
    err = [d - u.shortfall_usd_lo.astype(float) / 1e3, u.shortfall_usd_hi.astype(float) / 1e3 - d]
    ax.bar(x, d, color="#bfbfbf", edgecolor="black", hatch="//", yerr=err, capsize=4)
    ax.set_xticks(x, u.payer_id)
    ax.set_ylabel("Shortfall, $ thousands")
    ax.set_title("Shortfall dollars by payer (95% claim bootstrap)", fontsize=10)
    fig.suptitle("Underpayment against contract, simulated payers", fontsize=11)
    fig.tight_layout()
    p = figures_dir / "econ_underpayment_by_payer.png"
    fig.savefig(p, dpi=150)
    plt.close(fig)
    paths.append(str(p.relative_to(figures_dir.parent)))

    f = flt.sort_values("payer_id")
    fig, ax = plt.subplots(figsize=(7, 4))
    within = (f.float_total_usd - f.float_late_usd) / 1e3
    late_fp = f.float_late_first_pass_usd.fillna(0) / 1e3
    late_den = f.float_late_after_denial_usd.fillna(0) / 1e3
    xx = np.arange(len(f))
    ax.bar(xx, within, color="#d9d9d9", edgecolor="black", label="Within the 30-day window")
    ax.bar(xx, late_fp, bottom=within, color="#7f7f7f", edgecolor="black", label="Late, first-pass payments")
    ax.bar(xx, late_den, bottom=within + late_fp, color="#262626", edgecolor="black", hatch="..", label="Late, paid after a denial")
    ax.set_xticks(xx, f.payer_id)
    ax.set_ylabel("Value of float, $ thousands (paid x SOFR x days / 365)")
    ax.set_title("What payers earn by holding the hospital's cash (simulated)", fontsize=10)
    ax.legend(fontsize=8)
    fig.tight_layout()
    p = figures_dir / "econ_float_by_payer.png"
    fig.savefig(p, dpi=150)
    plt.close(fig)
    paths.append(str(p.relative_to(figures_dir.parent)))

    i = incent.sort_values("payer_id")
    fig, ax = plt.subplots(figsize=(7, 4))
    xx = np.arange(len(i))
    above = i.shortfall_above_threshold_usd / 1e3
    below = i.shortfall_below_threshold_usd / 1e3
    ax.bar(xx, above, color="#404040", edgecolor="black", label="Above break-even threshold (worth appealing)")
    ax.bar(xx, below, bottom=above, color="#d9d9d9", edgecolor="black", hatch="//", label="Below threshold (not worth appealing)")
    for k, t in enumerate(i.threshold_usd):
        ax.text(k, float(above.iloc[k] + below.iloc[k]) * 1.02, f"${t:,.0f}", ha="center", fontsize=8)
    ax.set_xticks(xx, i.payer_id)
    ax.set_ylabel("Shortfall on underpaid claims, $ thousands")
    ax.set_title("Appeal a claim when shortfall x P(win) > appeal cost\n(label = threshold per claim)", fontsize=10)
    ax.set_ylim(0, float((above + below).max()) * 1.35)
    ax.legend(fontsize=8, loc="upper right")
    fig.tight_layout()
    p = figures_dir / "econ_appeal_threshold.png"
    fig.savefig(p, dpi=150)
    plt.close(fig)
    paths.append(str(p.relative_to(figures_dir.parent)))
    return paths
