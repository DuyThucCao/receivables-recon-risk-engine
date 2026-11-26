"""Question 5 (Bank credit analyst): how much can we lend against these receivables?

An asset-based-lending (ABL) borrowing base on the reconciled open receivables, built the way
a lender's borrowing-base certificate is built:

1. **Collateral value.** Open claim lines at the certificate date (point in time, from matched
   remittances; ties to ``recon.claim_line_balance`` at the as-of date). Billed charges are not
   collectible value, so each line is valued at its *net collectible value* (NCV):
   ``billed x payer collection ratio`` if not yet adjudicated, or ``max(0, min(open, billed x
   ratio - paid))`` after a partial payment. The collection ratio is the ratio of sums
   paid / billed over claims paid in the previous 365 days (cashflow.paid_ratios).
2. **Eligibility waterfall** (first failing rule is recorded): DENIED (claim status DENIED),
   DISPUTED (APPEALED or PENDED), AGED (older than ``eligibility_max_age_days`` since
   submission), CROSS_AGED (all remaining lines of a payer whose aged share of NCV exceeds
   ``cross_age_pct``).
3. **Concentration limit.** No payer may exceed ``concentration_limit`` of the final eligible
   base: the base E solves E = sum_p min(e_p, L E) (largest fixed point, found by iteration).
4. **Availability** = advance rate x eligible - credit balances owed back to payers.
5. **Expected loss** per payer = PD x LGD x EAD. PD = NCV-weighted probability that the claim is
   written off, from the Markov chain with the time-in-state correction (markov.py);
   LGD = 1 - recovery, recovery = paid / expected NCV on claims written off in the previous
   year; EAD = eligible NCV after the concentration cap. Check: is EL covered by the lender's
   margin (1 - advance rate) x eligible?
6. **Interest-rate stress.** Facility rate = SOFR (public, config/public_calibration.yaml) +
   spread (financial assumption), ACT/360. For base, +100 bp, +200 bp, 25% slower payment and
   +200 bp with slower payment: annual interest on the drawn facility and the financing cost of
   waiting for open receivables to pay (NCV x P(paid) x rate x expected remaining days / 360,
   remaining days from the cashflow lag curve conditional on the claim's age). Slower payment
   also ages the book (age x 1.25) and so reduces eligibility.

Validation on simulated data: tie-out of the point-in-time balances; an out-of-sample check of
expected loss and collections on a past certificate against what happened by the as-of date;
and (evaluation only) predicted PD against the generator's ultimate outcome per claim.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .. import FIGURES_DIR, SQL_DIR, labels
from . import cashflow, markov
from .markov import label_table, mpl, query_df, to_date

SQL_FILE = SQL_DIR / "models" / "42_borrowing_base.sql"

DEFAULTS = {
    "certificate_date": None,          # None = as-of date
    "steady_state_date": "2025-09-30", # a date while the simulated hospital is still billing
    "series_start": "2024-09-30",
    "series_end": "2025-12-31",
    "lookback_days": 365,
    "slow_factor": 1.25,
    "rate_shocks_bp": [100, 200],
    "n_boot": 300,
}

ASSUMPTION_DEFAULTS = {
    "facility_spread": {
        "value": 0.0275, "label": labels.FINANCIAL_ASSUMPTION,
        "source": "Assumption (module default, requested for config/calibration.yaml in docs/requests.md R-CCC-2). "
                  "Margin over SOFR on the receivables facility; not sourced from a market quote."},
    "cross_age_pct": {
        "value": 0.50, "label": labels.FINANCIAL_ASSUMPTION,
        "source": "Assumption (module default, requested in docs/requests.md R-CCC-2). If more than this share of a "
                  "payer's NCV is past the age limit, all of that payer's receivables are ineligible (cross-age rule)."},
}


# ------------------------------------------------------------------------------------------
# pure decision logic (unit-tested on hand-built examples)
# ------------------------------------------------------------------------------------------
def eligibility(lines: pd.DataFrame, max_age: int, cross_age_pct: float) -> pd.Series:
    """First failing rule per line: DENIED, DISPUTED, AGED, CROSS_AGED, else ELIGIBLE.

    ``lines`` needs payer_id, claim_state, age_days, ncv.
    """
    reason = pd.Series("ELIGIBLE", index=lines.index, dtype=object)
    reason[lines["claim_state"].eq("DENIED")] = "DENIED"
    reason[(reason == "ELIGIBLE") & lines["claim_state"].isin(["APPEALED", "PENDED"])] = "DISPUTED"
    aged = lines["age_days"] > max_age
    reason[(reason == "ELIGIBLE") & aged] = "AGED"
    # cross-age test on the payer's open NCV that is not denied or disputed
    base = lines[reason.isin(["ELIGIBLE", "AGED"])]
    tot = base.groupby("payer_id")["ncv"].sum()
    old = base[base["age_days"] > max_age].groupby("payer_id")["ncv"].sum().reindex(tot.index, fill_value=0.0)
    share = (old / tot.where(tot > 0)).fillna(0.0)
    bad = set(share[share > cross_age_pct].index)
    reason[(reason == "ELIGIBLE") & lines["payer_id"].isin(bad)] = "CROSS_AGED"
    return reason


def concentration_cap(eligible: pd.Series, limit: float, tol: float = 0.005, max_iter: int = 1000) -> pd.Series:
    """Cap each payer at ``limit`` of the final base: E = sum_p min(e_p, limit * E).

    Iterating E_{n+1} = sum min(e_p, limit E_n) from E_0 = sum e_p decreases monotonically to the
    largest fixed point. If fewer than 1/limit payers carry eligible value the only fixed point
    is 0 (e.g. one payer and a 30% limit): a concentration limit then makes the base unusable,
    which is the rule working as written.
    """
    e = eligible.clip(lower=0.0).astype(float)
    E = float(e.sum())
    for _ in range(max_iter):
        En = float(np.minimum(e, limit * E).sum())
        if abs(En - E) <= tol:
            E = En
            break
        E = En
    return np.minimum(e, limit * E)


def borrowing_base_table(lines: pd.DataFrame, advance_rate: float, limit: float) -> pd.DataFrame:
    """Payer-level waterfall from line-level reasons (``reason`` column already set)."""
    g = lines[lines["ncv"] > 0].pivot_table(index="payer_id", columns="reason", values="ncv", aggfunc="sum", fill_value=0.0)
    for c in ("ELIGIBLE", "DENIED", "DISPUTED", "AGED", "CROSS_AGED"):
        if c not in g:
            g[c] = 0.0
    t = pd.DataFrame(index=g.index)
    t["gross_ncv"] = g[["ELIGIBLE", "DENIED", "DISPUTED", "AGED", "CROSS_AGED"]].sum(axis=1)
    t["inel_denied"], t["inel_disputed"] = g["DENIED"], g["DISPUTED"]
    t["inel_aged"], t["inel_cross_aged"] = g["AGED"], g["CROSS_AGED"]
    t["eligible_pre_cap"] = g["ELIGIBLE"]
    t["eligible"] = concentration_cap(t["eligible_pre_cap"], limit)
    t["concentration_excess"] = t["eligible_pre_cap"] - t["eligible"]
    cb = (-lines.loc[lines["open_balance"] < 0, "open_balance"]).groupby(lines["payer_id"]).sum()
    t["credit_balance_reserve"] = cb.reindex(t.index, fill_value=0.0)
    t["advance_rate"] = advance_rate
    t["availability"] = advance_rate * t["eligible"] - t["credit_balance_reserve"]
    return t


# ------------------------------------------------------------------------------------------
# data at a certificate date
# ------------------------------------------------------------------------------------------
def lines_asof(conn, d: date) -> pd.DataFrame:
    df = query_df(conn, "select * from mart.bb_lines_asof(%s)", (d,))
    for c in ("billed", "paid", "contractual", "denied", "patient_resp", "open_balance"):
        df[c] = df[c].astype(float)
    df["submission_date"] = pd.to_datetime(df["submission_date"])
    df["claim_state_since"] = pd.to_datetime(df["claim_state_since"])
    return df


def certificate(conn, d: Any, cfg: dict, prm: dict, *, with_risk: bool = True, age_factor: float = 1.0,
                seed: int = 0) -> dict[str, Any]:
    """Borrowing base (and optionally expected loss) at certificate date ``d`` using data <= d."""
    d = to_date(d)
    fa = cfg["financial_assumptions"]
    A = assumptions(cfg)
    max_age = int(fa["eligibility_max_age_days"]["value"])
    adv = float(fa["advance_rate"]["value"])
    lim = float(fa["concentration_limit"]["value"])
    xage = float(A["cross_age_pct"]["value"])
    D = pd.Timestamp(d)
    ln = lines_asof(conn, d)
    claims = markov.claims_asof(conn, d)
    ratios = cashflow.paid_ratios(claims, d, prm["lookback_days"])
    ros = {p: v["ratio_of_sums"] for p, v in ratios.items()}
    r = ln["payer_id"].map(lambda p: ros.get(p, ros["ALL"]))
    adjudicated = (ln["paid"] + ln["contractual"] + ln["denied"]).abs() > 0
    in_play = ln["claim_state"].isin(["DENIED", "APPEALED"])
    # denied lines in play are valued as if overturned (they are ineligible in any case)
    ln["ncv"] = np.where(in_play | ~adjudicated, ln["billed"] * r,
                         np.maximum(0.0, np.minimum(ln["open_balance"], ln["billed"] * r - ln["paid"])))
    ln.loc[ln["open_balance"] < -0.005, "ncv"] = 0.0
    keep = (ln["claim_state"] != "WRITTEN_OFF") & ((ln["open_balance"].abs() > 0.005) | in_play)
    ln = ln[keep].copy()
    ln["age_days"] = ((D - ln["submission_date"]).dt.days * age_factor).round().astype(int)
    ln["reason"] = eligibility(ln, max_age, xage)
    bb = borrowing_base_table(ln, adv, lim)
    out = {"date": d, "lines": ln, "table": bb, "ratios": ratios, "claims": claims,
           "rules": {"max_age_days": max_age, "advance_rate": adv, "concentration_limit": lim, "cross_age_pct": xage}}
    if with_risk:
        ev = markov.events_asof(conn, d)
        chain = markov.fit_from_events(ev, d, maturity_days=180)
        chain_usd = markov.fit_from_events(ev, d, maturity_days=180, claim_weights=claims.set_index("claim_id")["billed"])
        days_in_state = (D - ln["claim_state_since"]).dt.days.clip(lower=0)
        args = (ln["claim_state"].tolist(), ln["payer_id"].tolist(), days_in_state.tolist())
        ln["p_written_off_count_chain"] = 1.0 - chain.p_paid_open(*args)
        # PD on dollars: large claims are denied more often, so the count-weighted chain understates
        # the dollar share written off (seen in the expected-loss backtest); the lender loses dollars.
        ln["p_written_off"] = 1.0 - chain_usd.p_paid_open(*args)
        lgd = lgd_by_payer(claims, d, prm["lookback_days"], ratios, prm["n_boot"], seed)
        el_rows = {}
        for payer, row in bb.iterrows():
            el = ln[(ln["payer_id"] == payer) & (ln["reason"] == "ELIGIBLE")]
            w = el["ncv"].to_numpy()
            pd_ = float((w * el["p_written_off"]).sum() / w.sum()) if w.sum() > 0 else 0.0
            lg = lgd.get(payer, lgd["ALL"])["lgd"]
            ead = float(row["eligible"])
            el_rows[payer] = {"pd": pd_, "lgd": lg, "ead": ead, "expected_loss": pd_ * lg * ead}
        er = pd.DataFrame(el_rows).T
        for c in er:
            bb[c] = er[c]
        out.update({"chain": chain, "chain_usd": chain_usd, "lgd": lgd})
    return out


def lgd_by_payer(claims: pd.DataFrame, d: date, lookback: int, ratios: dict, n_boot: int, seed: int) -> dict[str, dict]:
    """LGD = 1 - recovery on claims written off in the lookback; recovery = paid / (billed x ratio)."""
    D = pd.Timestamp(d)
    wo = claims[(claims["state"] == "WRITTEN_OFF") & (claims["state_since"] > D - pd.Timedelta(days=lookback))].copy()
    ros = {p: v["ratio_of_sums"] for p, v in ratios.items()}
    wo["expected"] = wo["billed"] * wo["payer_id"].map(lambda p: ros.get(p, ros["ALL"]))
    wo["recovered"] = wo["paid_to_date"].clip(lower=0)
    rng = np.random.default_rng(seed)
    out = {}
    for payer, g in list(wo.groupby("payer_id")) + [("ALL", wo)]:
        if len(g) == 0 or g["expected"].sum() <= 0:
            continue
        rec = float(g["recovered"].sum() / g["expected"].sum())
        idx = rng.integers(0, len(g), size=(n_boot, len(g)))
        e, rc = g["expected"].to_numpy(), g["recovered"].to_numpy()
        bs = 1 - rc[idx].sum(axis=1) / e[idx].sum(axis=1)
        out[payer] = {"lgd": float(np.clip(1 - rec, 0, 1)), "recovery_rate": rec, "n_written_off_claims": int(len(g)),
                      "lgd_95": [float(np.clip(np.percentile(bs, 2.5), 0, 1)), float(np.clip(np.percentile(bs, 97.5), 0, 1))]}
    if "ALL" not in out:
        out["ALL"] = {"lgd": 1.0, "recovery_rate": 0.0, "n_written_off_claims": 0, "lgd_95": [1.0, 1.0]}
    return out


def assumptions(cfg: dict) -> dict[str, dict]:
    fa = (cfg or {}).get("financial_assumptions", {})
    return {k: (fa[k] if k in fa else v) for k, v in ASSUMPTION_DEFAULTS.items()}


# ------------------------------------------------------------------------------------------
# interest-rate stress
# ------------------------------------------------------------------------------------------
def expected_remaining_days(cdf: np.ndarray, age: int) -> float:
    """E[T - a | T > a] = sum_{t >= a} S(t) / S(a) for a discrete lag distribution with CDF ``cdf``."""
    M = len(cdf) - 1
    a = min(int(age), M - 1)
    S = 1.0 - cdf
    if S[a] <= 1e-12:
        return 0.0
    return float(S[a:M].sum() / S[a])


def stress(conn, cert: dict, cfg: dict, prm: dict) -> pd.DataFrame:
    d = cert["date"]
    sofr = float(cfg["discount_rate"]["value"])
    spread = float(assumptions(cfg)["facility_spread"]["value"])
    ln = cert["lines"]
    claims = cert["claims"]
    ld = cashflow.lag_data(claims, cert["chain"], d, prm["lookback_days"], cert["ratios"])
    lags = cashflow.lag_models(ld, dict(cashflow.DEFAULTS))
    biggest = max(lags, key=lambda k: lags[k]["n_claims"])
    rem = np.array([expected_remaining_days(lags.get(p, lags[biggest])["cdf"]["hybrid"], a)
                    for p, a in zip(ln["payer_id"], ln["age_days"])])
    value = ln["ncv"].to_numpy() * (1 - ln["p_written_off"].to_numpy())
    base_avail = float(cert["table"]["availability"].sum())
    slow = certificate(conn, d, cfg, prm, with_risk=False, age_factor=prm["slow_factor"]) if conn is not None else None
    slow_avail = float(slow["table"]["availability"].sum()) if slow is not None else base_avail
    rows = []
    scen = [("base", 0, 1.0)] + [(f"+{b}bp", b, 1.0) for b in prm["rate_shocks_bp"]] + \
           [(f"slower_payment_x{prm['slow_factor']}", 0, prm["slow_factor"]),
            (f"+{max(prm['rate_shocks_bp'])}bp_and_slower", max(prm["rate_shocks_bp"]), prm["slow_factor"])]
    for name, bp, fac in scen:
        rate = sofr + spread + bp / 1e4
        avail = slow_avail if fac != 1.0 else base_avail
        drawn = max(avail, 0.0)
        rows.append({"scenario": name, "rate_annual": rate, "drawn": drawn,
                     "interest_cost_annual": drawn * rate * 365 / 360,
                     "interest_cost_13w": drawn * rate * 91 / 360,
                     "carry_cost_of_delay": float((value * rate * rem * fac / 360).sum()),
                     "availability": avail})
    return pd.DataFrame(rows)


# ------------------------------------------------------------------------------------------
# out-of-sample checks
# ------------------------------------------------------------------------------------------
def tie_out(conn, as_of: date) -> dict[str, Any]:
    pit = lines_asof(conn, as_of)[["claim_line_id", "open_balance"]]
    rb = query_df(conn, "select claim_line_id, open_balance::float as ob from recon.claim_line_balance")
    m = pit.merge(rb, on="claim_line_id", how="outer").fillna(0.0)
    diff = m["open_balance"] - m["ob"]
    return {"lines": int(len(m)), "point_in_time_open_total": round(float(m["open_balance"].sum()), 2),
            "recon_claim_line_balance_open_total": round(float(m["ob"].sum()), 2),
            "difference_total": round(float(diff.sum()), 2), "lines_with_difference": int((diff.abs() > 0.005).sum()),
            "abs_difference_total": round(float(diff.abs().sum()), 2)}


def el_backtest(conn, cert: dict, eval_date: date) -> dict[str, Any]:
    """Predicted expected loss and collections on a past certificate vs what happened by eval_date."""
    ln = cert["lines"]
    el = ln[ln["reason"] == "ELIGIBLE"].copy()
    later = lines_asof(conn, eval_date).set_index("claim_line_id")
    el["state_end"] = later.loc[el["claim_line_id"], "claim_state"].to_numpy()
    el["paid_end"] = later.loc[el["claim_line_id"], "paid"].to_numpy()
    el["collected"] = (el["paid_end"] - el["paid"]).clip(lower=0)
    lgd = cert["lgd"]
    el["pred_loss"] = el["ncv"] * el["p_written_off"] * el["payer_id"].map(lambda p: lgd.get(p, lgd["ALL"])["lgd"])
    wo = el["state_end"] == "WRITTEN_OFF"
    realised_loss = float((el.loc[wo, "ncv"] - el.loc[wo, "collected"]).clip(lower=0).sum())
    still_open = ~el["state_end"].isin(["PAID", "WRITTEN_OFF"])
    return {"certificate_date": str(cert["date"]), "eval_date": str(eval_date), "eligible_lines": int(len(el)),
            "eligible_ncv": round(float(el["ncv"].sum()), 2),
            "predicted_expected_loss": round(float(el["pred_loss"].sum()), 2),
            "realised_loss_written_off": round(realised_loss, 2),
            "predicted_pd_ncv_weighted": round(float((el["ncv"] * el["p_written_off"]).sum() / el["ncv"].sum()), 4),
            "predicted_pd_ncv_weighted_count_chain": round(float((el["ncv"] * el["p_written_off_count_chain"]).sum() / el["ncv"].sum()), 4),
            "predicted_pd_count": round(float(el["p_written_off"].mean()), 4),
            "predicted_pd_count_chain_count": round(float(el["p_written_off_count_chain"].mean()), 4),
            "realised_write_off_rate_count": round(float(wo.mean()), 4),
            "realised_write_off_share_ncv": round(float(el.loc[wo, "ncv"].sum() / el["ncv"].sum()), 4),
            "lines_still_open_at_eval": int(still_open.sum()),
            "collected_after_certificate": round(float(el["collected"].sum()), 2),
            "collected_share_of_ncv": round(float(el["collected"].sum() / el["ncv"].sum()), 4)}


def evaluate_against_planted(cert: dict, claim_truth: pd.DataFrame) -> dict[str, Any]:
    """Predicted write-off probability of eligible lines vs the generator's ultimate outcome."""
    el = cert["lines"][cert["lines"]["reason"] == "ELIGIBLE"]
    t = claim_truth.set_index("claim_id")["final_state_ultimate"]
    y = el["claim_id"].map(t).eq("WRITTEN_OFF").astype(float)
    w = el["ncv"]
    out = {"certificate_date": str(cert["date"]), "n_lines": int(len(el)),
           "predicted_pd_count": round(float(el["p_written_off"].mean()), 4),
           "ultimate_write_off_rate_count": round(float(y.mean()), 4),
           "predicted_pd_ncv_weighted": round(float((w * el["p_written_off"]).sum() / w.sum()), 4),
           "ultimate_write_off_share_ncv": round(float((w * y).sum() / w.sum()), 4), "per_payer": {}}
    for p, g in el.groupby("payer_id"):
        yy = y[g.index]
        out["per_payer"][p] = {"n": int(len(g)), "predicted_pd": round(float(g["p_written_off"].mean()), 4),
                               "ultimate_write_off_rate": round(float(yy.mean()), 4)}
    return out


# ------------------------------------------------------------------------------------------
# marts, figures, run
# ------------------------------------------------------------------------------------------
def write_marts(conn, certs: list[dict], stress_tables: dict[date, pd.DataFrame]) -> None:
    conn.execute("truncate mart.borrowing_base, mart.borrowing_base_line, mart.borrowing_base_stress")
    with conn.cursor() as cur:
        for c in certs:
            t = c["table"]
            cur.executemany(
                "insert into mart.borrowing_base values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'model_estimate')",
                [(c["date"], p, round(r.gross_ncv, 2), round(r.inel_denied, 2), round(r.inel_disputed, 2), round(r.inel_aged, 2),
                  round(r.inel_cross_aged, 2), round(r.eligible_pre_cap, 2), round(r.concentration_excess, 2), round(r.eligible, 2),
                  round(r.credit_balance_reserve, 2), float(r.advance_rate), round(r.availability, 2),
                  float(getattr(r, "pd")) if "pd" in t else None, float(r.lgd) if "lgd" in t else None,
                  round(float(r.ead), 2) if "ead" in t else None, round(float(r.expected_loss), 2) if "expected_loss" in t else None)
                 for p, r in t.iterrows()])
            if "p_written_off" in c["lines"]:
                ln = c["lines"]
                cur.executemany("insert into mart.borrowing_base_line values (%s,%s,%s,%s,%s,%s,%s,%s,%s,'model_estimate')",
                                [(c["date"], int(r.claim_line_id), r.payer_id, int(r.age_days), r.claim_state,
                                  round(r.open_balance, 2), round(r.ncv, 2), r.reason, float(r.p_written_off)) for r in ln.itertuples()])
        for d, st in stress_tables.items():
            cur.executemany("insert into mart.borrowing_base_stress values (%s,%s,%s,%s,%s,%s,%s,%s,'model_estimate')",
                            [(d, r.scenario, float(r.rate_annual), round(r.drawn, 2), round(r.interest_cost_annual, 2),
                              round(r.interest_cost_13w, 2), round(r.carry_cost_of_delay, 2), round(r.availability, 2))
                             for r in st.itertuples()])
    label_table(conn, "mart.borrowing_base", labels.MODEL_ESTIMATE,
                "ABL borrowing base by certificate date and payer: NCV, eligibility waterfall, concentration cap, availability, PD x LGD x EAD. Advance rate and limits are financial_assumption.")
    label_table(conn, "mart.borrowing_base_line", labels.MODEL_ESTIMATE,
                "Line-level eligibility (first failing rule) and Markov write-off probability at each certificate date.")
    label_table(conn, "mart.borrowing_base_stress", labels.MODEL_ESTIMATE,
                "Facility interest cost and financing cost of collection delay under rate and slower-payment scenarios (SOFR observed_public, spread financial_assumption).")


def figures(certs: list[dict], series: pd.DataFrame, head: dict, st: pd.DataFrame, fig_dir: Path = FIGURES_DIR) -> list[str]:
    plt = mpl()
    fig_dir.mkdir(parents=True, exist_ok=True)
    out = []
    # 1. waterfall at the steady-state certificate
    t = head["table"].sum(numeric_only=True)
    steps = [("gross NCV", t["gross_ncv"]), ("denied", -t["inel_denied"]), ("disputed", -t["inel_disputed"]),
             ("aged", -t["inel_aged"]), ("cross-aged", -t["inel_cross_aged"]), ("concentration", -t["concentration_excess"]),
             ("eligible", None), ("advance haircut", -(t["eligible"] - t["eligible"] * head["rules"]["advance_rate"])),
             ("credit balances", -t["credit_balance_reserve"]), ("availability", None)]
    fig, ax = plt.subplots(figsize=(7.2, 3.6))
    level = 0.0
    for i, (name, v) in enumerate(steps):
        if v is None:
            ax.bar(i, level / 1e6, color="0.25")
            ax.text(i, level / 1e6, f"{level / 1e6:.2f}", ha="center", va="bottom", fontsize=7)
        else:
            bottom = level if v >= 0 else level + v
            ax.bar(i, abs(v) / 1e6, bottom=bottom / 1e6, color="0.6" if v < 0 else "0.25", edgecolor="black", lw=0.5)
            level += v
    ax.set_xticks(range(len(steps)), [s[0] for s in steps], rotation=30, ha="right")
    ax.set_ylabel("$ million")
    ax.set_title(f"Borrowing base waterfall, certificate {head['date']}\nmodel_estimate; advance rate and limits are financial_assumption",
                 fontsize=9)
    fig.tight_layout()
    p = fig_dir / "credit_borrowing_base_waterfall.png"
    fig.savefig(p)
    plt.close(fig)
    out.append(str(p))
    # 2. series of availability
    fig, ax = plt.subplots(figsize=(6.6, 3.4))
    x = pd.to_datetime(series["date"])
    ax.plot(x, series["gross_ncv"] / 1e6, color="0.6", ls="--", label="gross NCV")
    ax.plot(x, series["eligible"] / 1e6, color="0.35", label="eligible after limits")
    ax.plot(x, series["availability"] / 1e6, color="black", lw=2, label="availability")
    ax.set_ylabel("$ million")
    ax.set_xlabel("certificate date (month end)")
    ax.set_title("Borrowing base over time (point in time, no look-ahead)\nmodel_estimate; billing stops after 2025-12-31",
                 fontsize=9)
    ax.legend(fontsize=7)
    fig.autofmt_xdate()
    fig.tight_layout()
    p = fig_dir / "credit_availability_series.png"
    fig.savefig(p)
    plt.close(fig)
    out.append(str(p))
    # 3. stress
    fig, ax = plt.subplots(figsize=(6.6, 3.4))
    xs = np.arange(len(st))
    ax.bar(xs - 0.2, st["interest_cost_annual"] / 1e3, width=0.4, color="0.3", label="facility interest, annual")
    ax.bar(xs + 0.2, st["carry_cost_of_delay"] / 1e3, width=0.4, color="0.7", edgecolor="black", lw=0.5,
           label="financing cost of collection delay")
    ax.set_xticks(xs, st["scenario"], rotation=20, ha="right")
    ax.set_ylabel("$ thousand")
    ax.set_title(f"Interest-rate and slower-payment stress, certificate {head['date']}\nmodel_estimate; SOFR observed_public + spread assumption",
                 fontsize=9)
    ax.legend(fontsize=7)
    fig.tight_layout()
    p = fig_dir / "credit_stress.png"
    fig.savefig(p)
    plt.close(fig)
    out.append(str(p))
    return out


def _params(cfg: dict) -> dict:
    p = dict(DEFAULTS)
    p.update((cfg or {}).get("credit", {}))
    return p


def _cert_summary(c: dict) -> dict:
    t = c["table"]
    tot = t.sum(numeric_only=True)
    per = {p: {k: (round(float(v), 4) if k in ("pd", "lgd", "advance_rate") else round(float(v), 2)) for k, v in r.items()}
           for p, r in t.iterrows()}
    reasons = c["lines"].groupby("reason")["ncv"].agg(["count", "sum"]).round(2)
    out = {"certificate_date": str(c["date"]), "rules": c["rules"],
           "totals": {k: round(float(tot[k]), 2) for k in ("gross_ncv", "inel_denied", "inel_disputed", "inel_aged", "inel_cross_aged",
                                                             "eligible_pre_cap", "concentration_excess", "eligible",
                                                             "credit_balance_reserve", "availability") if k in tot},
           "lines_by_reason": {k: {"n": int(v["count"]), "ncv": float(v["sum"])} for k, v in reasons.iterrows()},
           "per_payer": per}
    if "expected_loss" in t:
        out["totals"]["expected_loss"] = round(float(t["expected_loss"].sum()), 2)
        margin = float(tot["eligible"]) * (1 - c["rules"]["advance_rate"])
        out["lender_margin_eligible_x_(1-advance)"] = round(margin, 2)
        out["expected_loss_covered_by_margin"] = bool(float(t["expected_loss"].sum()) <= margin)
        out["lgd_detail"] = {k: {kk: (round(vv, 4) if isinstance(vv, float) else vv) for kk, vv in v.items()} for k, v in c["lgd"].items()}
    return out


def run(conn, cfg: dict) -> dict:
    prm = _params(cfg)
    seed = int(cfg.get("seed", 0))
    as_of = to_date(cfg["as_of_date"])
    d_head = to_date(prm["certificate_date"] or as_of)
    d_ss = to_date(prm["steady_state_date"])
    markov.ensure_sql(conn)
    markov.run_sql_file(conn, SQL_FILE)
    tie = tie_out(conn, as_of)
    head = certificate(conn, d_head, cfg, prm, seed=seed)
    ss = certificate(conn, d_ss, cfg, prm, seed=seed)
    st_head, st_ss = stress(conn, head, cfg, prm), stress(conn, ss, cfg, prm)
    # sensitivity: 90-day age limit at the steady-state date
    cfg90 = {**cfg, "financial_assumptions": {**cfg["financial_assumptions"],
                                              "eligibility_max_age_days": {"value": 90, "label": labels.FINANCIAL_ASSUMPTION, "source": "sensitivity"}}}
    ss90 = certificate(conn, d_ss, cfg90, prm, with_risk=False)
    # monthly series
    series_rows, d = [], to_date(prm["series_start"])
    while d <= min(to_date(prm["series_end"]), as_of):
        c = certificate(conn, d, cfg, prm, with_risk=False)
        tt = c["table"].sum(numeric_only=True)
        series_rows.append({"date": str(d), "gross_ncv": float(tt["gross_ncv"]), "eligible": float(tt["eligible"]),
                            "availability": float(tt["availability"])})
        d = (pd.Timestamp(d) + pd.offsets.MonthEnd(1)).date()
    series = pd.DataFrame(series_rows)
    bt = el_backtest(conn, ss, as_of)
    write_marts(conn, [head, ss], {d_head: st_head, d_ss: st_ss})
    figs = figures([head, ss], series, ss, st_ss)
    M, E, FA = labels.MODEL_ESTIMATE, labels.SYNTHETIC_RECORD, labels.FINANCIAL_ASSUMPTION
    fa = cfg["financial_assumptions"]
    rate_inputs = {"sofr": cfg["discount_rate"], **assumptions(cfg)}
    return {
        "module": "recon_engine.models.credit",
        "question": "Q5 Bank credit analyst: how much can we lend against these receivables?",
        "label_note": "Simulated data for a fictional hospital. Results show that the borrowing-base logic behaves correctly "
                      "and how well risk inputs are recovered; they are not findings about real payers, lenders or rates.",
        "inputs": {"tables": ["core.fact_claim_line", "core.fact_remit_line", "core.fact_claim_event", "recon.claim_remit_match",
                              "recon.remit_duplicate", "recon.claim_line_balance (tie-out)"],
                   "sql": ["sql/models/40_chain.sql", "sql/models/42_borrowing_base.sql"], "params": prm},
        "seed": seed,
        "cutoff": str(d_head),
        "assumptions": {"advance_rate": fa["advance_rate"], "eligibility_max_age_days": fa["eligibility_max_age_days"],
                        "concentration_limit": fa["concentration_limit"], **rate_inputs},
        "tie_out_point_in_time_vs_recon": labels.stamp(tie, E, note="open balance per line from mart.bb_lines_asof(as_of) vs recon.claim_line_balance"),
        "certificate_as_of": labels.stamp(_cert_summary(head), M,
                                          note="At the as-of date the simulated hospital has not billed for three months, so almost all open "
                                               "receivables are past the age limit: a run-off book."),
        "certificate_steady_state": labels.stamp(_cert_summary(ss), M, note="certificate while the simulated hospital is still billing"),
        "sensitivity_90_day_limit_steady_state": labels.stamp(_cert_summary(ss90)["totals"], M),
        "stress_steady_state": labels.stamp(st_ss.round(4).to_dict(orient="records"), M),
        "stress_as_of": labels.stamp(st_head.round(4).to_dict(orient="records"), M),
        "availability_series": labels.stamp(series.round(2).to_dict(orient="records"), M),
        "expected_loss_backtest": labels.stamp(bt, M, note="eligible pool of the steady-state certificate followed to the as-of date"),
        "marts": ["mart.borrowing_base", "mart.borrowing_base_line", "mart.borrowing_base_stress"],
        "figures": figs,
    }
