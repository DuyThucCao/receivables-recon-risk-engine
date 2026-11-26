"""Question 6 (pharmacy manager): which drugs lose money after reimbursement?

Pipeline (all heavy lifting in SQL, ``sql/models/60-62``):

1. ``60_drug_inputs.sql``  lots and dispense events from core (plus a labelled imputation for
   dispense rows staging rejected because their units were unreadable).
2. ``61_drug_fifo.sql``    FIFO lot costing by a range-overlap join on cumulative units; expired
   units are charged to the lot that expired; cents allocated by telescoping rounding so that
   purchased = administered + wasted + on hand per lot, to the unit and the cent.
3. ``62_drug_margin.sql``  margin per drug = payer paid - FIFO cost - expired waste, with an
   additive decomposition into price gap, waste, denials, pending payment, patient share and
   pricing below contract. The CMS Part B payment limit (ASP-based) is a reference line only.

Everything is simulated except the drug names, HCPCS codes and CMS payment limits. Purchase
prices, volumes, payers and payments are synthetic. Results show that the logic works, not
anything about real drugs or payers.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

from .. import CONFIG_DIR, FIGURES_DIR, SQL_DIR
from .. import labels as L

NAME = "drug"
SQL_FILES = ["60_drug_inputs.sql", "61_drug_fifo.sql", "62_drug_margin.sql"]
SENS_DENIAL_RATES = (0.05, 0.10, 0.15, 0.20, 0.25, 0.30)
SENS_CONTRACT_PCT = (1.00, 1.15, 1.25, 1.35, 1.50)   # contract rate as a multiple of the CMS payment limit
MATURE_DAYS = 180          # financial_assumption: run-out after which a drug claim counts as settled

MART_LABELS = {
    "mart.drug_fifo": (L.SYNTHETIC_RECORD, "FIFO allocation of simulated dispense events to simulated purchase lots."),
    "mart.drug_fifo_piece": (L.SYNTHETIC_RECORD, "Every piece of every lot (administered, expired, on hand) with cent-exact cost."),
    "mart.drug_fifo_unallocated": (L.SYNTHETIC_RECORD, "Dispensed units with no purchased unit behind them (should be empty)."),
    "mart.drug_lot_position": (L.SYNTHETIC_RECORD, "Per lot: purchased = administered + wasted + on hand, units and dollars."),
    "mart.drug_margin": (L.MODEL_ESTIMATE, "Margin per drug: reconciled payer paid - FIFO cost - expired waste; ASP payment limit as reference."),
    "mart.drug_margin_payer": (L.MODEL_ESTIMATE, "Contract rate per unit by payer against FIFO average cost per unit."),
    "mart.drug_reference_price": (L.PUBLISHED_AGGREGATE, "CMS Medicare Part B payment limit per HCPCS billing unit (reference, not acquisition cost)."),
}


# ------------------------------------------------------------------------------------------
def _frame(conn, sql: str, params: tuple = ()) -> pd.DataFrame:
    cur = conn.execute(sql, params)
    cols = [d.name for d in cur.description]
    return pd.DataFrame(cur.fetchall(), columns=cols)


def _f(x: Any) -> float | None:
    return None if x is None or (isinstance(x, float) and pd.isna(x)) else float(x)


def run_sql(conn, name: str) -> None:
    conn.execute((SQL_DIR / "models" / name).read_text(encoding="utf-8"))


PUBLIC_CALIBRATION = CONFIG_DIR / "public_calibration.yaml"


def load_reference_prices(conn, path: Path = PUBLIC_CALIBRATION) -> dict:
    """Write mart.drug_reference_price from the public CMS payment-limit list in
    config/public_calibration.yaml (section drug_reference_prices). Only public reference prices
    are read; nothing about simulated acquisition costs. If the file or section is missing the
    table stays empty and the ASP columns of the margin mart are NULL."""
    conn.execute("drop table if exists mart.drug_reference_price")
    conn.execute("""create table mart.drug_reference_price (hcpcs text primary key, short_description text,
                    dosage text, payment_limit_usd numeric(14,4), therapeutic_area text, label text)""")
    sec = {}
    if path.exists():
        sec = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("drug_reference_prices") or {}
    drugs = (sec.get("drugs") or []) if sec.get("status", "ok") != "unavailable" else []
    label = sec.get("source_class", L.PUBLISHED_AGGREGATE)
    with conn.cursor() as cur:
        cur.executemany("insert into mart.drug_reference_price values (%s, %s, %s, %s, %s, %s)",
                        [(d["hcpcs"], d["short_description"], d["dosage"], float(d["payment_limit_usd"]),
                          d.get("therapeutic_area"), label) for d in drugs])
    return {"n_drugs": len(drugs), "label": label, "quarter": sec.get("quarter"),
            "source": sec.get("source", "config/public_calibration.yaml absent"), "caveat": sec.get("caveat")}


# ------------------------------------------------------------------------------------------
def run(conn, cfg: dict) -> dict:
    as_of = date.fromisoformat(str(cfg["as_of_date"]))
    mature_cutoff = as_of - timedelta(days=MATURE_DAYS)
    conn.execute("drop table if exists mart.drug_fifo_param")
    conn.execute("create table mart.drug_fifo_param as select %s::date as as_of, %s::date as mature_cutoff",
                 (as_of, mature_cutoff))
    ref_info = load_reference_prices(conn)
    for f in SQL_FILES:
        run_sql(conn, f)
    for t, (lab, note) in MART_LABELS.items():
        L.label_table(conn, t, lab, note)

    checks = validation_checks(conn)
    margin = _frame(conn, "select * from mart.drug_margin order by cohort, margin_collected")
    out = {
        "name": NAME,
        "question": "Q6 Pharmacy manager: which drugs lose money after reimbursement?",
        "label_note": "Drug names, HCPCS codes and CMS payment limits are real public references; purchase prices, "
                      "volumes, payers and payments are simulated for a fictional hospital.",
        "seed": cfg.get("seed"),
        "cutoff": {"as_of_date": as_of.isoformat(), "mature_cutoff": mature_cutoff.isoformat(),
                   "mature_days": L.stamp(MATURE_DAYS, L.FINANCIAL_ASSUMPTION,
                                          note="claims with service date at least this many days before as-of count as settled")},
        "inputs": {"tables": ["core.fact_drug_purchase", "core.fact_drug_dispense", "core.fact_claim_line",
                              "core.dim_drug", "recon.claim_line_balance", "recon.claim_line_status",
                              "recon.claim_remit_match", "core.fact_remit_line", "stg.reject (dispense rows only)"],
                   "sql": [f"sql/models/{f}" for f in SQL_FILES],
                   "reference_prices": ref_info},
        "method": "FIFO by receipt date via range-overlap join on cumulative units; expired units charged to their lot; "
                  "telescoping cent rounding per lot; margin = payer paid - FIFO cost - expired waste.",
        "validation": checks,
        "totals": totals(conn),
        "headline": headline(margin[margin.cohort == "MATURE"]),
        "sensitivity_mature": sensitivity(margin[margin.cohort == "MATURE"]),
        "drugs_mature": drug_rows(margin[margin.cohort == "MATURE"]),
        "drugs_all": drug_rows(margin[margin.cohort == "ALL"]),
        "losers_mature": losers(margin[margin.cohort == "MATURE"]),
        "contract_below_cost_by_payer": below_cost_by_payer(conn),
        "figures": make_figures(margin) + [sensitivity_figure(margin[margin.cohort == "MATURE"])],
        "marts": {t: v[0] for t, v in MART_LABELS.items()},
    }
    return out


# ------------------------------------------------------------------------------------------
def validation_checks(conn) -> dict:
    """Internal checks that need no generator records (those live in scripts/run_models_econ.py)."""
    lot = _frame(conn, """
        select count(*) as n_lots,
               bool_and(units_identity_ok) as units_ok, bool_and(value_identity_ok) as value_ok,
               sum(purchased_units) as pu, sum(administered_units) as au, sum(wasted_units) as wu, sum(on_hand_units) as ou,
               sum(purchased_value) as pv, sum(administered_cost) as ac, sum(wasted_cost) as wc, sum(on_hand_value) as ov,
               sum(infeasible_units) as infeasible_units,
               count(*) filter (where expired_still_on_hand) as expired_on_hand_lots
        from mart.drug_lot_position""").iloc[0]
    by_drug = _frame(conn, """
        select hcpcs, sum(purchased_value) - sum(administered_cost) - sum(wasted_cost) - sum(on_hand_value) as diff_value,
               sum(purchased_units) - sum(administered_units) - sum(wasted_units) - sum(on_hand_units) as diff_units
        from mart.drug_lot_position group by hcpcs""")
    ev = _frame(conn, """
        select (select coalesce(sum(units), 0) from mart.drug_fifo_event_input where event_type = 'ADMINISTER') as admin_units_in,
               (select coalesce(sum(qty), 0) from mart.drug_fifo where event_type = 'ADMINISTER') as admin_units_allocated,
               (select coalesce(sum(units), 0) from mart.drug_fifo_event_input where event_type = 'WASTE_EXPIRED') as waste_units_in,
               (select coalesce(sum(qty), 0) from mart.drug_fifo where event_type = 'WASTE_EXPIRED') as waste_units_allocated,
               (select count(*) from mart.drug_fifo_unallocated) as n_unallocated,
               (select count(*) from mart.drug_fifo_event_input where source = 'IMPUTED_FROM_CLAIM_LINE') as n_imputed,
               (select coalesce(sum(units), 0) from mart.drug_fifo_event_input where source = 'IMPUTED_FROM_CLAIM_LINE') as imputed_units,
               (select count(*) from mart.drug_fifo where method = 'SPECIFIC_LOT') as n_waste_specific""").iloc[0]
    # FIFO against the lot the pharmacist recorded (specific identification), where recorded.
    spec = _frame(conn, """
        with rec as (
            select f.event_id, f.qty, f.cost, f.lot_no as fifo_lot, e.lot_no as recorded_lot, e.units as event_units
            from mart.drug_fifo f join mart.drug_fifo_event_input e using (event_id)
            where f.event_type = 'ADMINISTER' and e.lot_no is not null
        ), ev as (
            select r.event_id, max(r.event_units) as units, sum(r.cost) as fifo_cost, max(r.recorded_lot) as recorded_lot
            from rec r group by r.event_id
        )
        select (select sum(qty) filter (where fifo_lot = recorded_lot) / sum(qty) from rec) as share_units_on_recorded_lot,
               sum(ev.fifo_cost) as fifo_cost,
               sum(round(ev.units * li.lot_value / li.units, 2)) as specific_id_cost,
               count(*) as n_events
        from ev join mart.drug_fifo_lot_input li on li.lot_no = ev.recorded_lot""").iloc[0]
    claim_tie = _frame(conn, """
        select d.hcpcs_code, coalesce(c.u, 0) as claim_units, coalesce(e.u, 0) as dispensed_units
        from core.dim_drug d
        left join (select procedure_code, sum(units) u from core.fact_claim_line group by 1) c on c.procedure_code = d.hcpcs_code
        left join (select hcpcs, sum(units) u from mart.drug_fifo_event_input where event_type = 'ADMINISTER' group by 1) e
               on e.hcpcs = d.hcpcs_code""")
    S = L.SYNTHETIC_RECORD
    return {
        "lot_identity_units_all_lots": L.stamp(bool(lot.units_ok), S, note="purchased = administered + wasted + on hand, every lot, to the unit"),
        "lot_identity_value_all_lots": L.stamp(bool(lot.value_ok), S, note="same identity in dollars, every lot, to the cent"),
        "drug_identity_max_abs_diff_value": L.stamp(float(by_drug.diff_value.abs().max()), S),
        "drug_identity_max_abs_diff_units": L.stamp(float(by_drug.diff_units.abs().max()), S),
        "n_lots": L.stamp(int(lot.n_lots), S),
        "units": {"purchased": L.stamp(float(lot.pu), S), "administered": L.stamp(float(lot.au), S),
                  "expired_waste": L.stamp(float(lot.wu), S), "on_hand": L.stamp(float(lot.ou), S)},
        "dollars": {"purchased": L.stamp(float(lot.pv), S), "administered_fifo_cost": L.stamp(float(lot.ac), S),
                    "expired_waste": L.stamp(float(lot.wc), S), "on_hand": L.stamp(float(lot.ov), S)},
        "every_event_unit_allocated": L.stamp(bool(ev.admin_units_in == ev.admin_units_allocated
                                                   and ev.waste_units_in == ev.waste_units_allocated
                                                   and ev.n_unallocated == 0), S),
        "waste_events_charged_to_their_lot": L.stamp(int(ev.n_waste_specific), S),
        "dispense_rows_imputed_from_claim_line": L.stamp(int(ev.n_imputed), S,
                                                         note=f"{float(ev.imputed_units):.0f} units; staging rejected their unreadable units"),
        "fifo_infeasible_units": L.stamp(float(lot.infeasible_units), L.MODEL_ESTIMATE,
                                         note="units FIFO places on a lot before its receipt or after its expiry date: "
                                              "where FIFO differs from the physical first-expiry-first-out flow"),
        "expired_lots_still_on_hand": L.stamp(int(lot.expired_on_hand_lots), S),
        "fifo_vs_recorded_lot": {
            "share_of_units_on_recorded_lot": L.stamp(round(float(spec.share_units_on_recorded_lot), 4), L.MODEL_ESTIMATE),
            "fifo_cost": L.stamp(float(spec.fifo_cost), L.MODEL_ESTIMATE),
            "specific_identification_cost": L.stamp(float(spec.specific_id_cost), L.MODEL_ESTIMATE),
            "difference_pct": L.stamp(round(100 * (float(spec.fifo_cost) / float(spec.specific_id_cost) - 1), 4), L.MODEL_ESTIMATE),
            "n_events": int(spec.n_events),
            "note": "cost of administrations whose lot is recorded, FIFO versus the recorded lot's unit cost",
        },
        "claim_units_vs_dispensed_units_max_abs_diff": L.stamp(
            float((claim_tie.claim_units - claim_tie.dispensed_units).abs().max()), S,
            note="by drug; differences come from claim lines or dispense rows lost in staging"),
    }


def breakeven_denial_rate(paid: float, expected: float, denied: float, cost: float) -> float:
    """Dollar-weighted final-denial rate at which margin = 0.

    paid P, contract value E, contract value on finally denied lines D, cost C (+ waste).
    Non-denied contract dollars collect at k = P / (E - D), so margin(d) = k E (1 - d) - C and
        d* = 1 - C / (k E).
    """
    k = paid / (expected - denied)
    return 1.0 - cost / (k * expected)


def margin_at(paid, expected, denied, cost, units_billed, asp, denial_rate=None, contract_pct=None):
    """Counterfactual margin per drug (arrays allowed): margin = k (1 - d) E' - C, where
    E' = contract_pct x ASP x units (or the observed E when contract_pct is None) and d is the
    final-denial rate (observed D / E when denial_rate is None)."""
    paid, expected, denied, cost = (np.asarray(x, dtype=float) for x in (paid, expected, denied, cost))
    k = paid / (expected - denied)
    d = denied / expected if denial_rate is None else denial_rate
    e = expected if contract_pct is None else contract_pct * np.asarray(asp, dtype=float) * np.asarray(units_billed, dtype=float)
    return k * (1 - d) * e - cost


def _sens_inputs(m: pd.DataFrame) -> tuple:
    m = m.dropna(subset=["asp_payment_limit"])
    return (m.payer_paid.astype(float), m.expected_allowed.astype(float), m.denied_leak.astype(float),
            (m.fifo_cogs.astype(float) + m.waste_cost.astype(float)), m.units_billed.astype(float),
            m.asp_payment_limit.astype(float))


def headline(m: pd.DataFrame) -> dict:
    P, E, D, C = (float(m[c].astype(float).sum()) for c in ("payer_paid", "expected_allowed", "denied_leak", "fifo_cogs"))
    C += float(m.waste_cost.astype(float).sum())
    cpr = (m.expected_allowed.astype(float) / m.units_billed.astype(float) / m.asp_payment_limit.astype(float))
    cost_r = m.cost_to_asp.astype(float)
    M = L.MODEL_ESTIMATE
    d_obs, d_star = D / E, breakeven_denial_rate(P, E, D, C)
    return {
        "statement": L.stamp(
            f"Under the simulation's stated assumptions (contract rates {cpr.min():.0%}-{cpr.max():.0%} of the CMS payment "
            f"limit on average per drug, acquisition cost {cost_r.min():.0%}-{cost_r.max():.0%} of it, and a simulated "
            f"dollar-weighted final-denial rate of {d_obs:.1%} on drug claim lines), {int((m.margin_collected.astype(float) < 0).sum())} "
            f"of {len(m)} drugs lose money; the drug book as a whole turns profitable when the dollar-weighted final-denial "
            f"rate falls below {d_star:.1%}. The loss is driven by the denial assumption, not by price. A real hospital "
            f"with 340B acquisition pricing or higher commercial rates would see different results.", M),
        "denied_dollar_share_observed": L.stamp(round(d_obs, 4), L.SYNTHETIC_RECORD,
                                                note="contract value on finally denied drug lines / contract value"),
        "breakeven_denial_share_total": L.stamp(round(d_star, 4), M),
        "collection_ratio_non_denied": L.stamp(round(P / (E - D), 4), M,
                                               note="payer paid per contract dollar on lines not finally denied"),
        "n_drugs_breakeven_below_observed": L.stamp(int((m.breakeven_denial_share.astype(float) < m.denied_dollar_share.astype(float)).sum()), M),
        "contract_pct_of_reference_range": [round(float(cpr.min()), 4), round(float(cpr.max()), 4)],
        "acquisition_pct_of_reference_range": [round(float(cost_r.min()), 4), round(float(cost_r.max()), 4)],
        "assumption_note": "Break-even holds non-denied collection at the observed ratio (patient share counted as not "
                           "collected); contract rates and acquisition prices are simulated, not observed.",
    }


def sensitivity(m: pd.DataFrame) -> dict:
    P, E, D, C, U, A = _sens_inputs(m)
    by_denial = [{"final_denial_rate": d, "total_margin": round(float(margin_at(P, E, D, C, U, A, denial_rate=d).sum()), 2)}
                 for d in SENS_DENIAL_RATES]
    by_pct = [{"contract_pct_of_reference": t, "total_margin": round(float(margin_at(P, E, D, C, U, A, contract_pct=t).sum()), 2)}
              for t in SENS_CONTRACT_PCT]
    grid = [{"final_denial_rate": d, "contract_pct_of_reference": t,
             "total_margin": round(float(margin_at(P, E, D, C, U, A, denial_rate=d, contract_pct=t).sum()), 2)}
            for d in SENS_DENIAL_RATES for t in SENS_CONTRACT_PCT]
    return {"label": L.MODEL_ESTIMATE,
            "note": "Counterfactual total margin, MATURE cohort: margin = k (1 - d) E' - C - W per drug, summed. "
                    "Rows vary one input with the other at its simulated value; the grid varies both "
                    "(contract rate set to the same multiple of the CMS payment limit for every drug).",
            "observed": {"final_denial_rate": round(float(D.sum() / E.sum()), 4),
                         "total_margin": round(float((P - C).sum()), 2)},
            "by_final_denial_rate": by_denial, "by_contract_pct_of_reference": by_pct, "grid": grid}


def totals(conn) -> dict:
    t = _frame(conn, """select cohort, sum(payer_paid) paid, sum(patient_resp) pr, sum(fifo_cogs) cogs, sum(waste_cost) waste,
                               sum(margin_collected) margin, sum(expected_allowed) expected, sum(denied_leak) denied,
                               sum(pending_leak) pending, sum(pricing_gap) pricing, sum(price_gap) price_gap,
                               count(*) filter (where margin_collected < 0) n_losing, count(*) n_drugs
                        from mart.drug_margin group by cohort""")
    out = {}
    for _, r in t.iterrows():
        S, M = L.SYNTHETIC_RECORD, L.MODEL_ESTIMATE
        out[r.cohort] = {"payer_paid": L.stamp(_f(r.paid), S), "patient_resp": L.stamp(_f(r.pr), S),
                         "fifo_cogs": L.stamp(_f(r.cogs), M), "expired_waste": L.stamp(_f(r.waste), M),
                         "margin_collected": L.stamp(_f(r.margin), M), "contract_expected": L.stamp(_f(r.expected), S),
                         "price_gap_contract_minus_cost": L.stamp(_f(r.price_gap), M),
                         "denied_leak": L.stamp(_f(r.denied), S), "pending_leak": L.stamp(_f(r.pending), S),
                         "pricing_gap": L.stamp(_f(r.pricing), S),
                         "n_drugs_losing_money": L.stamp(int(r.n_losing), M), "n_drugs": int(r.n_drugs)}
    return out


def drug_rows(df: pd.DataFrame) -> dict:
    cols = ["hcpcs", "short_description", "dosage", "asp_payment_limit", "units_administered", "payer_paid",
            "patient_resp", "fifo_cogs", "waste_cost", "margin_collected", "margin_if_patient_pays", "margin_per_unit",
            "fifo_cost_per_unit", "contract_per_unit", "collected_per_unit", "cost_to_asp", "contract_to_asp",
            "collected_to_asp", "price_gap", "denied_leak", "pending_leak", "pricing_gap", "denied_line_share",
            "median_days_to_pay", "avg_shelf_life_days", "waste_share_of_purchases", "denied_dollar_share",
            "collection_ratio_non_denied", "breakeven_denial_share", "primary_driver",
            "primary_driver_amount", "secondary_driver"]
    rows = []
    for _, r in df.iterrows():
        rows.append({c: (r[c] if isinstance(r[c], str) or r[c] is None else _f(r[c])) for c in cols})
    return {"label": L.MODEL_ESTIMATE, "rows": rows}


def losers(df: pd.DataFrame) -> dict:
    d = df[df.margin_collected < 0].sort_values("margin_collected")
    return {"label": L.MODEL_ESTIMATE,
            "ranked": [{"hcpcs": r.hcpcs, "description": r.short_description,
                        "margin_collected": _f(r.margin_collected), "margin_per_unit": _f(r.margin_per_unit),
                        "primary_driver": r.primary_driver, "primary_driver_amount": _f(r.primary_driver_amount),
                        "secondary_driver": r.secondary_driver, "cost_to_asp": _f(r.cost_to_asp),
                        "contract_to_asp": _f(r.contract_to_asp), "denied_line_share": _f(r.denied_line_share),
                        "denied_dollar_share": _f(r.denied_dollar_share), "breakeven_denial_share": _f(r.breakeven_denial_share)}
                       for r in d.itertuples()]}


def below_cost_by_payer(conn) -> dict:
    """Per payer: drugs whose contract rate per unit is below the drug's FIFO average cost (MATURE cohort)."""
    df = _frame(conn, """
        select payer_id, count(*) as n_drugs,
               count(*) filter (where contract_minus_cost_per_unit < 0) as n_drugs_rate_below_cost,
               coalesce(sum(contract_minus_cost_per_unit * units_billed) filter (where contract_minus_cost_per_unit < 0), 0)
                   as loss_at_contract_usd,
               string_agg(hcpcs, ', ' order by contract_minus_cost_per_unit) filter (where contract_minus_cost_per_unit < 0)
                   as drugs
        from mart.drug_margin_payer group by payer_id order by payer_id""")
    return {"label": L.MODEL_ESTIMATE,
            "note": "even if every claim were paid in full at contract, these payer x drug pairs lose money per unit",
            "rows": [{"payer_id": r.payer_id, "n_drugs": int(r.n_drugs), "n_drugs_rate_below_cost": int(r.n_drugs_rate_below_cost),
                      "loss_at_contract_usd": round(float(r.loss_at_contract_usd), 2), "drugs": r.drugs}
                     for r in df.itertuples()]}


# ------------------------------------------------------------------------------------------
def sensitivity_figure(m: pd.DataFrame, figures_dir: Path = FIGURES_DIR) -> str:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    P, E, D, C, U, A = _sens_inputs(m)
    d = np.linspace(0.0, 0.35, 36)
    fig, ax = plt.subplots(figsize=(7.5, 4.5))
    styles = ["-", "--", "-.", ":", (0, (5, 1))]
    greys = ["#000000", "#404040", "#606060", "#808080", "#a0a0a0"]
    for t, ls, g in zip(SENS_CONTRACT_PCT, styles, greys):
        y = [margin_at(P, E, D, C, U, A, denial_rate=x, contract_pct=t).sum() / 1e6 for x in d]
        ax.plot(100 * d, y, linestyle=ls, color=g, label=f"Contract = {t:.0%} of payment limit")
    y_obs = [margin_at(P, E, D, C, U, A, denial_rate=x).sum() / 1e6 for x in d]
    ax.plot(100 * d, y_obs, color="black", linewidth=2.5, label="Contract as simulated (per drug)")
    d0 = float(D.sum() / E.sum())
    ax.plot([100 * d0], [float((P - C).sum()) / 1e6], marker="o", markersize=9, color="black", linestyle="none",
            label=f"Simulated: {d0:.1%} denied, margin as reported")
    ax.axhline(0, color="black", linewidth=0.8)
    ax.set_xlabel("Dollar-weighted final-denial rate on drug claim lines, %")
    ax.set_ylabel("Total drug margin, $ millions (MATURE cohort)")
    ax.set_title("Drug margin under other denial rates and contract levels\n"
                 "(simulated costs and payments; counterfactual, not a forecast)", fontsize=10)
    ax.legend(fontsize=7, loc="lower left")
    fig.tight_layout()
    p = figures_dir / "drug_margin_sensitivity.png"
    fig.savefig(p, dpi=150)
    plt.close(fig)
    return str(p.relative_to(figures_dir.parent))


def make_figures(margin: pd.DataFrame, figures_dir: Path = FIGURES_DIR) -> list[str]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figures_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    m = margin[margin.cohort == "MATURE"].copy()
    for c in ("margin_collected", "cost_to_asp", "collected_to_asp", "contract_to_asp"):
        m[c] = m[c].astype(float)
    m["label"] = m.hcpcs + " " + m.short_description.fillna("").str.slice(0, 22)

    # 1. ranked margin
    d = m.sort_values("margin_collected")
    fig, ax = plt.subplots(figsize=(8, 8))
    colors = ["#404040" if v < 0 else "#bdbdbd" for v in d.margin_collected]
    hatches = ["//" if v < 0 else "" for v in d.margin_collected]
    bars = ax.barh(d.label, d.margin_collected / 1e3, color=colors, edgecolor="black", linewidth=0.5)
    for b, h in zip(bars, hatches):
        b.set_hatch(h)
    ax.axvline(0, color="black", linewidth=0.8)
    ax.set_xlabel("Margin collected, $ thousands (payer paid - FIFO cost - expired waste)")
    ax.set_title("Margin by drug, service dates up to as-of minus 180 days\n"
                 "Simulated purchases and payments; drug names are real references", fontsize=10)
    ax.tick_params(axis="y", labelsize=7)
    fig.tight_layout()
    p = figures_dir / "drug_margin_ranked.png"
    fig.savefig(p, dpi=150)
    plt.close(fig)
    paths.append(str(p.relative_to(figures_dir.parent)))

    # 2. unit economics relative to the CMS payment limit
    d = m.sort_values("cost_to_asp")
    fig, ax = plt.subplots(figsize=(8, 8))
    y = range(len(d))
    ax.scatter(d.cost_to_asp, y, marker="s", color="black", label="FIFO acquisition cost per unit")
    ax.scatter(d.contract_to_asp, y, marker="^", facecolor="white", edgecolor="black", label="Contract rate per unit (all payers)")
    ax.scatter(d.collected_to_asp, y, marker="o", color="#808080", label="Collected per unit (payer paid)")
    ax.axvline(1.0, color="black", linestyle="--", linewidth=1, label="CMS Part B payment limit (reference)")
    ax.set_yticks(list(y))
    ax.set_yticklabels(d.label, fontsize=7)
    ax.set_xlabel("Dollars per billing unit as a multiple of the CMS Part B payment limit")
    ax.set_title("Unit economics per drug (simulated costs and payments)\n"
                 "Payment limit = Medicare reference price, not acquisition cost", fontsize=10)
    ax.legend(fontsize=7, loc="upper center", bbox_to_anchor=(0.5, -0.07), ncol=2, frameon=False)
    fig.tight_layout()
    p = figures_dir / "drug_unit_economics.png"
    fig.savefig(p, dpi=150)
    plt.close(fig)
    paths.append(str(p.relative_to(figures_dir.parent)))
    return paths
