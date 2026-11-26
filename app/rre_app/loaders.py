"""Data loading for every screen: plain functions of a psycopg connection, no Streamlit.

Each ``load_<screen>(conn)`` returns a dict. Values are DataFrames (money as float), result
dictionaries read from ``results/*.json``, or ``None``. Marts that a model has not built yet are
listed under ``"missing"`` so the screen can say "not built yet" instead of failing. Only
documented mart names are read (docs/DESIGN.md section 6, docs/data_dictionary.md, model notes).

Interactive recalculations call the model modules' own functions (``prioritize.choose``,
``credit.eligibility`` / ``credit.borrowing_base_table``, ``cashflow.forecast`` /
``cashflow.shortfall_metrics``, ``econ.appeal_threshold`` / ``econ.expected_net_recovery``) so the
app never re-implements a model differently.
"""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd

from .paths import RESULTS_DIR

# ------------------------------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------------------------------


def frame(conn, q: str, params: tuple | dict = ()) -> pd.DataFrame:
    cur = conn.execute(q, params)
    df = pd.DataFrame(cur.fetchall(), columns=[d.name for d in cur.description])
    for c in df.columns:
        s = df[c].dropna()
        if len(s) and isinstance(s.iloc[0], Decimal):
            df[c] = df[c].astype(float)
    return df


def existing_tables(conn, schema: str = "mart") -> set[str]:
    rows = conn.execute("""select table_schema || '.' || table_name from information_schema.tables
                           where table_schema = %s""", (schema,)).fetchall()
    return {r[0] for r in rows}


def results(name: str) -> dict | None:
    """``results/<name>.json`` or None when the model has not written it yet."""
    p = RESULTS_DIR / f"{name}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def val(x: Any) -> Any:
    """Unwrap a labelled number ``{"value": v, "label": ...}``."""
    return x.get("value") if isinstance(x, dict) and "value" in x and "label" in x else x


def table_labels(conn) -> dict[str, str]:
    if "mart.table_label" not in existing_tables(conn):
        return {}
    return dict(conn.execute("select table_name, label from mart.table_label").fetchall())


def payer_names(conn) -> dict[str, str]:
    return dict(conn.execute("select payer_id, payer_name from core.dim_payer order by payer_id").fetchall())


def _need(conn, names: list[str]) -> tuple[set[str], list[str]]:
    have = existing_tables(conn)
    return have, [n for n in names if n not in have]


def model_config() -> dict:
    from recon_engine.datagen.calibration import model_config as mc
    return mc()


def as_of(conn) -> str:
    return conn.execute("select recon.param('as_of_date')").fetchone()[0]


# ------------------------------------------------------------------------------------------
# reconciliation desk
# ------------------------------------------------------------------------------------------
KEY_CHECKS = ("CONTROL_IDENTITY_TOTAL", "CASH_REMIT_SIDE", "CASH_BANK_SIDE", "BANK_OPENING_PLUS_ACTIVITY")


def desk_snapshot(conn) -> dict[str, Any]:
    """The numbers the desk compares before and after an intake."""
    ident = frame(conn, """select count(*) as n_lines, sum(billed) as billed, sum(paid) as paid,
                                  sum(contractual) as contractual, sum(denied) as denied,
                                  sum(patient_resp) as patient_resp, sum(open_balance) as open_balance
                           from recon.claim_line_balance""").iloc[0].to_dict()
    ct = frame(conn, """select check_id, check_name, scope, expected, actual, difference, passed, detail
                        from recon.control_totals order by check_id""")
    exc = frame(conn, """select exception_type, count(*) as n, coalesce(sum(amount), 0) as amount
                         from recon.exception_queue where status = 'OPEN' group by 1 order by 1""")
    days = conn.execute("""select count(*) filter (where status = 'CLOSED'), count(*),
                                  max(recon_date) filter (where status = 'CLOSED') from recon.daily_recon""").fetchone()
    stuck = frame(conn, "select bucket, n_items, amount from mart.cash_stuck order by sort")
    rules = frame(conn, """select rule_id, stage, priority, description, amount_tolerance, date_window_days, active
                           from recon.match_rule order by stage, priority""")
    return {"identity": ident, "controls": ct, "all_passed": bool(ct["passed"].all()) if len(ct) else False,
            "exceptions": exc, "days_closed": days[0], "days_total": days[1], "last_closed": days[2],
            "cash_stuck": stuck, "rules": rules, "as_of": as_of(conn)}


def intake_history(conn) -> pd.DataFrame:
    """Batches landed on the desk since the copy was made (raw rows whose file label is an intake)."""
    return frame(conn, """
        select split_part(_file, ':', 1) as batch, split_part(_file, ':', 2) as document, count(*) as rows,
               min(_loaded_at) as loaded_at
        from (select _file, _loaded_at from raw.remittance_advice where _file like '%%:%%'
              union all select _file, _loaded_at from raw.bank_statement where _file like '%%:%%'
              union all select _file, _loaded_at from raw.claims_export where _file like '%%:%%') x
        group by 1, 2 order by loaded_at""")


def compare_snapshots(before: dict, after: dict) -> pd.DataFrame:
    """Side-by-side table of the control identity and the stuck-cash buckets."""
    rows = []
    for k in ("billed", "paid", "contractual", "denied", "patient_resp", "open_balance"):
        rows.append({"item": f"Claim lines: {k.replace('_', ' ')}", "before": before["identity"][k],
                     "after": after["identity"][k]})
    b = before["cash_stuck"].set_index("bucket")["amount"]
    a = after["cash_stuck"].set_index("bucket")["amount"]
    for k in a.index:
        rows.append({"item": f"Stuck: {k}", "before": float(b.get(k, 0.0)), "after": float(a[k])})
    df = pd.DataFrame(rows)
    df["change"] = df["after"] - df["before"]
    return df


# ------------------------------------------------------------------------------------------
# Q1 CFO
# ------------------------------------------------------------------------------------------
Q1_MARTS = ["mart.cash_position", "mart.cash_stuck", "mart.ar_aging", "mart.pnl_by_payer_service"]


def load_cfo(conn) -> dict[str, Any]:
    have, missing = _need(conn, Q1_MARTS)
    out: dict[str, Any] = {"missing": missing, "as_of": as_of(conn)}
    if missing:
        return out
    out["cash_position"] = frame(conn, "select * from mart.cash_position order by month")
    out["cash_stuck"] = frame(conn, "select sort, bucket, description, n_items, amount, billed_basis from mart.cash_stuck order by sort")
    out["ar_aging"] = frame(conn, "select * from mart.ar_aging order by payer_id, aging_bucket")
    pnl = frame(conn, "select * from mart.pnl_by_payer_service")
    out["pnl"] = pnl
    money = ["billed", "contractual_adjustment", "denied", "patient_responsibility", "payer_paid", "open_balance",
             "contract_expected", "underpaid_vs_contract", "net_revenue_recognised", "drug_cost_avg_basis",
             "margin_after_drug_cost"]
    out["pnl_by_payer"] = pnl.groupby("payer_id")[money + ["n_lines"]].sum().reset_index()
    out["pnl_by_service"] = pnl.groupby("service_line_code")[money + ["n_lines"]].sum().reset_index()
    out["totals"] = pnl[money].sum().to_dict()
    cp = out["cash_position"]
    out["cash_totals"] = cp[["billed_charges", "payer_paid_per_remittance", "payer_cash_deposited", "payer_cash_matched",
                             "unapplied_cash"]].sum().to_dict()
    out["daily_recon"] = frame(conn, """select recon_date, payer_credits, payer_credits_matched, payer_credits_unapplied,
                                               remit_total, remit_matched_same_day, remit_timing_difference, remit_unmatched,
                                               closing_balance_statement, balance_difference, status
                                        from recon.daily_recon order by recon_date""")
    out["payers"] = frame(conn, "select payer_id, payer_name, payer_type from core.dim_payer order by payer_id")
    return out


# ------------------------------------------------------------------------------------------
# Q2 revenue-cycle lead
# ------------------------------------------------------------------------------------------
Q2_MARTS = ["mart.work_queue", "mart.work_queue_backtest"]


def load_revcycle(conn) -> dict[str, Any]:
    have, missing = _need(conn, Q2_MARTS)
    out: dict[str, Any] = {"missing": missing, "results": results("prioritize"), "payers": payer_names(conn)}
    if "mart.work_queue" in have:
        out["queue"] = frame(conn, "select * from mart.work_queue order by queue_rank")
    if "mart.work_queue_backtest" in have:
        out["backtest"] = frame(conn, "select * from mart.work_queue_backtest order by pool, hours_per_day, policy")
    cfg = model_config()
    fa = cfg["financial_assumptions"]
    out["staff_hours_default"] = float(fa["denial_staff_hours_per_day"]["value"])
    out["assumptions"] = {k: fa[k] for k in ("appeal_handling_cost_usd", "appeal_handling_minutes", "denial_staff_hours_per_day")}
    return out


def solve_worklist(queue: pd.DataFrame, hours: float) -> dict[str, Any]:
    """Re-solve today's list for ``hours`` of staff time with the model's own ``prioritize.choose``
    (exact 0/1 knapsack) and compare with oldest-first and largest-first on the same budget."""
    from recon_engine.models import prioritize

    q = queue.copy()
    q["denial_date"] = pd.to_datetime(q["denial_date"])
    q = q.sort_values("claim_id").reset_index(drop=True)  # same ordering as prioritize.run
    cap = int(round(hours * 60))
    out = {}
    picks = {}
    for pol in prioritize.POLICIES:
        idx = prioritize.choose(q, pol, cap)
        s = q.iloc[idx]
        picks[pol] = set(s["claim_id"])
        out[pol] = {"n_worked": int(len(s)), "minutes": int(s["handling_minutes"].sum()),
                    "expected_net": float(s["expected_value"].sum())}
    q["work_today"] = q["claim_id"].isin(picks["MODEL_KNAPSACK"])
    q["oldest_first_pick"] = q["claim_id"].isin(picks["OLDEST_FIRST"])
    q["largest_first_pick"] = q["claim_id"].isin(picks["LARGEST_FIRST"])
    q = q.sort_values(["work_today", "expected_value"], ascending=[False, False]).reset_index(drop=True)
    return {"capacity_minutes": cap, "policies": out, "list": q}


# ------------------------------------------------------------------------------------------
# Q3 treasurer
# ------------------------------------------------------------------------------------------
Q3_MARTS = ["mart.cash_forecast", "mart.cash_forecast_backtest"]


def load_treasurer(conn) -> dict[str, Any]:
    have, missing = _need(conn, Q3_MARTS)
    out: dict[str, Any] = {"missing": missing, "results": results("cashflow")}
    if "mart.cash_forecast" in have:
        out["forecast"] = frame(conn, "select * from mart.cash_forecast order by forecast_date, week_no")
    if "mart.cash_forecast_backtest" in have:
        out["backtest"] = frame(conn, "select * from mart.cash_forecast_backtest order by lag_model, forecast_date, week_no")
    from recon_engine.models import cashflow
    cfg = model_config()
    out["assumptions"] = cashflow.assumptions(cfg)
    return out


def steady_state_date() -> str:
    """The going-concern forecast date the cash model reports as its headline (last backtest date)."""
    from recon_engine.models import cashflow
    return str(cashflow._params(model_config())["backtest_end"])


def simulate_receipts(conn, forecast_date: str, n_sims: int | None = None) -> dict[str, Any]:
    """Run the model's own Monte Carlo (``cashflow.forecast``) at ``forecast_date`` with the seeds the
    pipeline uses (seed at the as-of date, seed + 1000 at the steady-state date), so those two dates
    reproduce results/cashflow.json. Also returns the median weekly receipts of the trailing 26
    weeks (``cashflow.cash_need``), the anchor of the weekly cash need."""
    from datetime import date, timedelta

    from recon_engine.models import cashflow

    cfg = model_config()
    prm = cashflow._params(cfg)
    f = date.fromisoformat(str(forecast_date))
    seed = int(cfg.get("seed", 0)) + (1000 if str(f) == str(prm["backtest_end"]) else 0)
    fc = cashflow.forecast(conn, f, cfg, prm, n_sims=n_sims, seed=seed)
    opening = float(conn.execute("select mart.cash_opening_balance(%s)", (f,)).fetchone()[0])
    _, median_receipts = cashflow.cash_need(conn, f, 1.0)
    asof = date.fromisoformat(as_of(conn))
    K = fc["sims"].shape[1]
    actual = cashflow.actual_weekly(conn, f, K) if f + timedelta(days=7 * K) <= asof else None
    return {"sims": fc["sims"], "opening": opening, "quantiles": cashflow.quantiles(fc["sims"]),
            "expected": fc["expected"], "actual": actual, "forecast_date": str(f), "median_receipts": median_receipts,
            "run_off": f > date.fromisoformat(str(cfg["service_end"]))}


def shortfall(sim: dict, weekly_need: float, minimum_balance: float) -> dict[str, Any]:
    from recon_engine.models import cashflow
    return cashflow.shortfall_metrics(sim["sims"], sim["opening"], weekly_need, minimum_balance)


# ------------------------------------------------------------------------------------------
# Q4 payer actuary
# ------------------------------------------------------------------------------------------
Q4_MARTS = ["mart.dev_triangle", "mart.reserve_factors", "mart.reserve_by_period", "mart.reserve_retro"]


def load_actuary(conn) -> dict[str, Any]:
    have, missing = _need(conn, Q4_MARTS)
    out: dict[str, Any] = {"missing": missing, "results": results("reserve")}
    if "mart.dev_triangle" in have:
        tri = frame(conn, "select * from mart.dev_triangle where grain = 'month' order by accident_month, dev_lag")
        out["triangle"] = tri
    if "mart.reserve_factors" in have:
        out["factors"] = frame(conn, "select * from mart.reserve_factors order by dev_lag_from")
    if "mart.reserve_by_period" in have:
        out["by_period"] = frame(conn, "select * from mart.reserve_by_period order by accident_month")
    if "mart.reserve_retro" in have:
        out["retro"] = frame(conn, "select * from mart.reserve_retro order by retro_as_of")
    return out


# ------------------------------------------------------------------------------------------
# Q5 bank credit analyst
# ------------------------------------------------------------------------------------------
Q5_MARTS = ["mart.borrowing_base", "mart.borrowing_base_line", "mart.borrowing_base_stress"]


def load_credit(conn) -> dict[str, Any]:
    have, missing = _need(conn, Q5_MARTS)
    out: dict[str, Any] = {"missing": missing, "results": results("credit"), "payers": payer_names(conn)}
    if "mart.borrowing_base" in have:
        out["base"] = frame(conn, "select * from mart.borrowing_base order by certificate_date, payer_id")
        out["certificate_dates"] = [str(d) for d in sorted(out["base"]["certificate_date"].unique())]
    if "mart.borrowing_base_stress" in have:
        out["stress"] = frame(conn, "select * from mart.borrowing_base_stress order by certificate_date, rate_annual")
    from recon_engine.models import credit
    cfg = model_config()
    fa = cfg["financial_assumptions"]
    out["assumptions"] = {"advance_rate": fa.get("advance_rate"), "concentration_limit": fa.get("concentration_limit"),
                          "eligibility_max_age_days": fa.get("eligibility_max_age_days"),
                          **credit.assumptions(cfg), "sofr": cfg["discount_rate"]}
    return out


def credit_lines(conn, certificate_date: str) -> pd.DataFrame:
    return frame(conn, """select claim_line_id, payer_id, age_days, claim_state, open_balance, ncv, reason, p_written_off
                          from mart.borrowing_base_line where certificate_date = %s""", (certificate_date,))


def recompute_borrowing_base(lines: pd.DataFrame, base: pd.DataFrame, advance_rate: float, concentration_limit: float,
                             max_age_days: int, cross_age_pct: float) -> pd.DataFrame:
    """Payer waterfall for user-chosen assumptions, using ``credit.eligibility`` and
    ``credit.borrowing_base_table``. PD and LGD per payer stay the model's (from the mart); EAD is
    the recomputed eligible amount, so EL = PD x LGD x EAD."""
    from recon_engine.models import credit

    ln = lines.copy()
    ln["reason"] = credit.eligibility(ln, int(max_age_days), float(cross_age_pct))
    t = credit.borrowing_base_table(ln, float(advance_rate), float(concentration_limit)).reset_index()
    risk = base[["payer_id", "pd", "lgd"]]
    t = t.merge(risk, on="payer_id", how="left")
    t["ead"] = t["eligible"]
    t["expected_loss"] = t["pd"].fillna(0) * t["lgd"].fillna(0) * t["ead"]
    return t


# ------------------------------------------------------------------------------------------
# Q6 pharmacy manager
# ------------------------------------------------------------------------------------------
Q6_MARTS = ["mart.drug_margin", "mart.drug_lot_position"]


def load_pharmacy(conn) -> dict[str, Any]:
    have, missing = _need(conn, Q6_MARTS)
    out: dict[str, Any] = {"missing": missing, "results": results("drug")}
    if "mart.drug_margin" in have:
        out["margin"] = frame(conn, "select * from mart.drug_margin order by cohort, margin_collected")
    if "mart.drug_lot_position" in have:
        out["lots"] = frame(conn, "select * from mart.drug_lot_position order by hcpcs, received_date, lot_no")
    if "mart.drug_margin_payer" in have:
        out["by_payer"] = frame(conn, "select * from mart.drug_margin_payer order by hcpcs, payer_id")
    return out


# ------------------------------------------------------------------------------------------
# Q7 consulting economist
# ------------------------------------------------------------------------------------------
Q7_MARTS = ["mart.underpayment", "mart.float_value", "mart.underpayment_incentive", "mart.econ_appeal_outcome",
            "mart.underpayment_claim"]


def load_economist(conn) -> dict[str, Any]:
    have, missing = _need(conn, Q7_MARTS)
    out: dict[str, Any] = {"missing": missing, "results": results("econ"), "payers": payer_names(conn)}
    for name in Q7_MARTS:
        if name in have and name != "mart.underpayment_claim":
            out[name.split(".")[1]] = frame(conn, f"select * from {name} order by payer_id")
    if "mart.underpayment_claim" in have:
        out["claims"] = frame(conn, """select payer_id, claim_id, pricing_shortfall as s from mart.underpayment_claim
                                       where is_underpaid""")
    cfg = model_config()
    out["appeal_cost_default"] = float(cfg["financial_assumptions"]["appeal_handling_cost_usd"]["value"])
    out["appeal_cost_assumption"] = cfg["financial_assumptions"]["appeal_handling_cost_usd"]
    return out


def appeal_policy(claims: pd.DataFrame, appeals: pd.DataFrame, appeal_cost: float) -> pd.DataFrame:
    """Per payer: break-even threshold c / P(win) and the expected net recovery of appealing every
    underpaid claim above it, with ``econ.appeal_threshold`` and ``econ.expected_net_recovery``."""
    from recon_engine.models import econ

    rows = []
    for _, a in appeals.iterrows():
        w = float(a["win_rate"]) if a["win_rate"] is not None and not pd.isna(a["win_rate"]) else float("nan")
        s = claims.loc[claims["payer_id"] == a["payer_id"], "s"].to_numpy(float)
        if not (0 < w <= 1):
            continue
        thr = econ.appeal_threshold(appeal_cost, w)
        above = s > thr
        rows.append({"payer_id": a["payer_id"], "p_win": w, "threshold_usd": thr, "n_underpaid_claims": int(len(s)),
                     "n_above_threshold": int(above.sum()), "shortfall_above_usd": float(s[above].sum()),
                     "shortfall_below_usd": float(s[~above].sum()),
                     "expected_net_recovery_usd": float(econ.expected_net_recovery(s[above], w, appeal_cost).sum())})
    return pd.DataFrame(rows)


# ------------------------------------------------------------------------------------------
# methods screen
# ------------------------------------------------------------------------------------------


def load_methods(conn) -> dict[str, Any]:
    from recon_engine import labels
    out = {"labels": labels.DESCRIPTIONS, "table_labels": table_labels(conn), "summary": None}
    from .paths import DOCS_DIR
    p = DOCS_DIR / "data_sources.md"
    out["data_sources_md"] = p.read_text(encoding="utf-8") if p.exists() else None
    out["core"] = results("core_build")
    out["reconciliation"] = results("reconciliation")
    return out


LOADERS = {
    "desk": desk_snapshot, "cfo": load_cfo, "revcycle": load_revcycle, "treasurer": load_treasurer,
    "actuary": load_actuary, "credit": load_credit, "pharmacy": load_pharmacy, "economist": load_economist,
    "methods": load_methods,
}

