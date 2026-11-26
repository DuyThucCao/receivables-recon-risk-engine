#!/usr/bin/env python
"""Run the drug (Q6) and economics (Q7) models on the built database, then score them against
what the generator planted.

    export RRE_PG_DIR=~/.cache/receivables-engine/pg-econ
    python run_all.py --skip-generate      # build the database
    python scripts/run_models_econ.py      # this script

Writes results/drug.json and results/econ.json (exactly what each model's run() returns), and
results/drug_validation.json and results/econ_validation.json (comparison with the generator's
records in data/truth/). Only this evaluation script reads data/truth/; the model modules never do.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recon_engine import FIGURES_DIR, RESULTS_DIR, db, labels as L  # noqa: E402
from recon_engine.cli import _jsonable  # noqa: E402
from recon_engine.datagen.calibration import model_config  # noqa: E402
from recon_engine.models import drug, econ  # noqa: E402

GEN_DIR = ROOT / "data" / "truth"   # generator records: evaluation only


def frame(conn, sql: str) -> pd.DataFrame:
    cur = conn.execute(sql)
    return pd.DataFrame(cur.fetchall(), columns=[d.name for d in cur.description])


# ------------------------------------------------------------------------------------------
def validate_drug(conn) -> dict:
    lots = pd.read_csv(GEN_DIR / "drug_lot_truth.csv")
    pos = frame(conn, """select lot_no, hcpcs, purchased_units::float8 pu, administered_units::float8 au,
                                wasted_units::float8 wu, on_hand_units::float8 ou from mart.drug_lot_position""")
    m = lots.merge(pos, on="lot_no", how="outer", indicator=True)
    both = m[m["_merge"] == "both"].copy()
    both["d_adm"] = both.administered_units - both.au
    both["d_waste"] = both.wasted_units - both.wu
    both["d_onhand"] = both.remaining_units - both.ou
    exact = (both.d_adm == 0) & (both.d_waste == 0) & (both.d_onhand == 0) & (both.units == both.pu)
    by_drug = both.groupby("hcpcs_x")[["d_adm", "d_waste", "d_onhand"]].sum()
    S, M = L.SYNTHETIC_RECORD, L.MODEL_ESTIMATE
    return {
        "note": "FIFO lot positions compared with the generator's physical lot records. The generator consumes "
                "first-expiry-first-out; FIFO is a cost-flow convention, so lot-level differences are expected "
                "where a later lot expires earlier. Drug-level totals must agree exactly.",
        "n_lots_generator": L.stamp(int(len(lots)), S),
        "n_lots_model": L.stamp(int(len(pos)), S),
        "n_lots_matched_by_lot_no": L.stamp(int(len(both)), S),
        "lots_only_in_one_side": L.stamp(int((m["_merge"] != "both").sum()), S),
        "purchased_units_equal_every_lot": L.stamp(bool((both.units == both.pu).all()), S),
        "share_lots_exact_all_quantities": L.stamp(round(float(exact.mean()), 4), M),
        "units_on_a_different_lot": L.stamp(float(both.d_adm.abs().sum() / 2), M,
                                            note="administered units FIFO places on another lot of the same drug"),
        "administered_units_generator": L.stamp(float(lots.administered_units.sum()), S),
        "administered_units_model": L.stamp(float(pos.au.sum()), S),
        "wasted_units_generator": L.stamp(float(lots.wasted_units.sum()), S),
        "wasted_units_model": L.stamp(float(pos.wu.sum()), S),
        "on_hand_units_generator": L.stamp(float(lots.remaining_units.sum()), S),
        "on_hand_units_model": L.stamp(float(pos.ou.sum()), S),
        "drug_totals_exact_all_drugs": L.stamp(bool((by_drug.abs().sum(axis=1) == 0).all()), M),
        "wasted_units_exact_every_lot": L.stamp(bool((both.d_waste == 0).all()), M),
    }


# ------------------------------------------------------------------------------------------
def validate_econ(conn, res: dict) -> tuple[dict, pd.DataFrame]:
    planted = json.loads((GEN_DIR / "planted_parameters.json").read_text())
    pp = planted["payers"]
    under = pd.DataFrame(res["underpayment_by_payer"]["rows"]).set_index("payer_id")
    inc = pd.DataFrame(res["incentives_by_payer"]["rows"]).set_index("payer_id")
    flt = pd.DataFrame(res["float_by_payer"]["rows"]).set_index("payer_id")

    # realised sample: the generator's per-claim flags on the claims the model saw as priced
    clt = pd.read_csv(GEN_DIR / "claim_line_truth.csv",
                      usecols=["claim_id", "line_no", "payer_id", "contract_allowed_cents",
                               "payer_allowed_cents_first_price", "underpaid", "shortfall_fraction"])
    lines = frame(conn, """select claim_id, line_no, payer_id, pricing_shortfall::float8 as s_model,
                                  expected_allowed::float8 as e_model, first_allowed::float8 as a1_model
                           from mart.underpayment_line""")
    j = lines.merge(clt, on=["claim_id", "line_no"], how="left", suffixes=("", "_gen"))
    j["s_gen"] = (j.contract_allowed_cents - j.payer_allowed_cents_first_price).clip(lower=0) / 100.0
    j["flag_model"] = j.s_model > 0
    j["flag_gen"] = j.s_gen >= 0.01
    tp = int((j.flag_model & j.flag_gen).sum())
    fp = int((j.flag_model & ~j.flag_gen).sum())
    fn = int((~j.flag_model & j.flag_gen).sum())
    claims = j.groupby(["payer_id", "claim_id"]).agg(u=("underpaid", "first"), f=("shortfall_fraction", "first")).reset_index()

    rows = []
    for p in sorted(pp):
        ub = pp[p]["underpayment"]
        c = claims[claims.payer_id == p]
        u = under.loc[p]
        lag = pp[p]["realised_first_pass_payment_lag_days"]
        rows.append({
            "payer_id": p,
            "planted_probability": ub["probability"],
            "realised_share_among_priced_claims": round(float(c.u.mean()), 5),
            "estimated_share": u.underpaid_claim_share, "share_lo": u.underpaid_claim_share_lo, "share_hi": u.underpaid_claim_share_hi,
            "planted_inside_ci": bool(u.underpaid_claim_share_lo <= ub["probability"] <= u.underpaid_claim_share_hi),
            "planted_mean_shortfall": ub["mean_shortfall"],
            "realised_mean_shortfall": round(float(c.loc[c.u.astype(bool), "f"].mean()), 5),
            "estimated_mean_shortfall": u.mean_shortfall_fraction, "mean_lo": u.mean_shortfall_fraction_lo,
            "mean_hi": u.mean_shortfall_fraction_hi,
            "planted_mean_inside_ci": bool(u.mean_shortfall_fraction_lo <= ub["mean_shortfall"] <= u.mean_shortfall_fraction_hi),
            "planted_overturn_target": pp[p]["overturn_target"], "estimated_appeal_win_rate": inc.loc[p, "p_win"],
            "win_rate_ci": [inc.loc[p, "p_win_lo"], inc.loc[p, "p_win_hi"]],
            "planted_inside_win_ci": bool(inc.loc[p, "p_win_lo"] <= pp[p]["overturn_target"] <= inc.loc[p, "p_win_hi"]),
            "generator_first_pass_lag_median": lag["median"], "model_first_pass_days_median": flt.loc[p, "median_days_first_pass"],
            "generator_first_pass_lag_mean": lag["mean"], "model_first_pass_days_mean": round(float(flt.loc[p, "mean_days_first_pass"]), 3),
        })
    per_payer = pd.DataFrame(rows)
    rank_planted = sorted(pp, key=lambda p: -pp[p]["underpayment"]["probability"] * pp[p]["underpayment"]["mean_shortfall"])
    rank_model = list(under.shortfall_rate.sort_values(ascending=False).index)
    M = L.MODEL_ESTIMATE
    out = {
        "note": "Estimates compared with the generator's planted parameters and with the realised draws on the "
                "claims the model saw. Planted values are simulation inputs, not facts about real payers.",
        "line_classification_vs_generator": {
            "true_positive": tp, "false_positive": fp, "false_negative": fn,
            "precision": L.stamp(round(tp / max(tp + fp, 1), 4), M),
            "recall": L.stamp(round(tp / max(tp + fn, 1), 4), M),
            "shortfall_usd_model": L.stamp(round(float(j.s_model.sum()), 2), M),
            "shortfall_usd_generator_first_price": L.stamp(round(float(j.s_gen.sum()), 2), L.SYNTHETIC_RECORD),
            "lines_without_generator_record": int(j.contract_allowed_cents.isna().sum()),
            "expected_allowed_equals_generator_contract_share": L.stamp(
                round(float((np.round(j.e_model * 100) == j.contract_allowed_cents).mean()), 5), M),
        },
        "per_payer": {"label": M, "rows": _jsonable(per_payer.to_dict(orient="records"))},
        "planted_share_inside_ci": L.stamp(int(per_payer.planted_inside_ci.sum()), M, note="of 6 payers"),
        "planted_mean_inside_ci": L.stamp(int(per_payer.planted_mean_inside_ci.sum()), M, note="of 6 payers"),
        "planted_win_rate_inside_ci": L.stamp(int(per_payer.planted_inside_win_ci.sum()), M, note="of 6 payers"),
        "ranking_by_planted_expected_shortfall": rank_planted,
        "ranking_by_estimated_shortfall_rate": rank_model,
        "top_payer_recovered": L.stamp(rank_planted[0] == rank_model[0], M),
    }
    return out, per_payer


def validation_figure(per_payer: pd.DataFrame) -> str:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    d = per_payer.sort_values("payer_id")
    x = np.arange(len(d))
    fig, axes = plt.subplots(1, 2, figsize=(10, 4))
    for ax, est, lo, hi, plant, real, title in (
            (axes[0], "estimated_share", "share_lo", "share_hi", "planted_probability",
             "realised_share_among_priced_claims", "Share of claims priced below contract"),
            (axes[1], "estimated_mean_shortfall", "mean_lo", "mean_hi", "planted_mean_shortfall",
             "realised_mean_shortfall", "Mean shortfall fraction of underpaid claims")):
        e = d[est].astype(float)
        ax.errorbar(x, e, yerr=[e - d[lo].astype(float), d[hi].astype(float) - e], fmt="o", color="black",
                    capsize=4, label="Estimate, 95% claim bootstrap")
        ax.scatter(x, d[plant], marker="x", s=60, color="#606060", label="Planted in generator")
        ax.scatter(x, d[real], marker="_", s=200, color="#a0a0a0", label="Realised in the simulated sample")
        ax.set_xticks(x, d.payer_id)
        ax.set_title(title, fontsize=10)
        ax.legend(fontsize=7)
    fig.suptitle("Parameter recovery: underpayment behaviour planted in the simulation", fontsize=11)
    fig.tight_layout()
    p = FIGURES_DIR / "econ_underpayment_recovery.png"
    fig.savefig(p, dpi=150)
    plt.close(fig)
    return str(p.relative_to(ROOT))


# ------------------------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-validate", action="store_true", help="skip the comparison with generator records")
    args = ap.parse_args(argv)
    cfg = model_config()
    RESULTS_DIR.mkdir(exist_ok=True)
    with db.connect() as conn:
        res_d = _jsonable(drug.run(conn, cfg))
        conn.commit()
        (RESULTS_DIR / "drug.json").write_text(json.dumps(res_d, indent=2, default=str), encoding="utf-8")
        res_e = _jsonable(econ.run(conn, cfg))
        conn.commit()
        (RESULTS_DIR / "econ.json").write_text(json.dumps(res_e, indent=2, default=str), encoding="utf-8")
        print("wrote results/drug.json, results/econ.json")
        if not args.no_validate:
            vd = validate_drug(conn)
            (RESULTS_DIR / "drug_validation.json").write_text(json.dumps(_jsonable(vd), indent=2, default=str), encoding="utf-8")
            ve, per_payer = validate_econ(conn, res_e)
            ve["figures"] = [validation_figure(per_payer)]
            (RESULTS_DIR / "econ_validation.json").write_text(json.dumps(_jsonable(ve), indent=2, default=str), encoding="utf-8")
            print("wrote results/drug_validation.json, results/econ_validation.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
