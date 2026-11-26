#!/usr/bin/env python
"""Run the operations-and-reserves models (Q2 prioritize, Q4 reserve) on the built database and
evaluate them against the generator's planted parameters.

    export RRE_PG_DIR=~/.cache/receivables-engine/pg-ops
    python run_all.py --skip-generate        # build the database first
    python scripts/run_models_ops.py

Writes
    results/prioritize.json, results/reserve.json            model outputs (no ground truth used)
    results/prioritize_evaluation.json, results/reserve_evaluation.json
                                                              comparison with data/truth (this script only)
    figures/prioritize_*.png, figures/reserve_*.png

This is an evaluation script: it is the only place in the Q2/Q4 area that opens data/truth. The
model modules never do (tests/test_core_pipeline_small.py checks their source text).
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recon_engine import FIGURES_DIR, RESULTS_DIR, db, labels  # noqa: E402
from recon_engine.cli import _jsonable  # noqa: E402
from recon_engine.datagen.calibration import model_config  # noqa: E402
from recon_engine.models import prioritize, reserve  # noqa: E402

TRUTH = ROOT / "data" / "truth"
ME, SR = labels.MODEL_ESTIMATE, labels.SYNTHETIC_RECORD


def _sigmoid(x):
    return 1.0 / (1.0 + np.exp(-x))


def _frame(conn, q, params=()):
    cur = conn.execute(q, params)
    df = pd.DataFrame(cur.fetchall(), columns=[d.name for d in cur.description])
    for c in df.columns:
        if df[c].dtype == object and len(df) and type(df[c].dropna().iloc[0]).__name__ == "Decimal":
            df[c] = df[c].astype(float)
    return df


# ------------------------------------------------------------------------------------------
# Q2 evaluation
# ------------------------------------------------------------------------------------------
def true_overturn_p(ep: pd.DataFrame, planted: dict, claim_truth: pd.DataFrame) -> np.ndarray:
    om = planted["overturn_model"]
    ln_center = planted["denial_model"]["ln_center"]
    ch = claim_truth.set_index("claim_id")["total_charge_cents"] / 100.0
    lnc = np.log(ep["claim_id"].map(ch).astype(float))
    eta = (ep["payer_id"].map({p: v["overturn_logit_intercept"] for p, v in planted["payers"].items()})
           + ep["reason_category"].map(om["reason"]) + ep["service_line_code"].map(om["service_line"])
           + om["amount"] * (lnc - ln_center))
    return _sigmoid(eta.to_numpy(float))


def evaluate_prioritize(conn, res: dict, cfg: dict) -> dict:
    from sklearn.metrics import roc_auc_score

    planted = json.loads((TRUTH / "planted_parameters.json").read_text())
    ct = pd.read_csv(TRUTH / "claim_truth.csv", usecols=["claim_id", "total_charge_cents"])
    om = planted["overturn_model"]
    ref = res["model"]["reference_levels"]

    # 1. parameter recovery: fitted contrasts vs planted contrasts (same reference levels)
    planted_contrast = {}
    for lev, v in om["reason"].items():
        if lev != ref["reason_category"]:
            planted_contrast[f"reason_category={lev}"] = v - om["reason"][ref["reason_category"]]
    for lev, v in om["service_line"].items():
        if lev != ref["service_line_code"]:
            planted_contrast[f"service_line_code={lev}"] = v - om["service_line"][ref["service_line_code"]]
    icpt = {p: v["overturn_logit_intercept"] for p, v in planted["payers"].items()}
    for p, v in icpt.items():
        if p != ref["payer_id"]:
            planted_contrast[f"payer_id={p}"] = v - icpt[ref["payer_id"]]
    planted_contrast["ln_amount_c"] = om["amount"]
    planted_contrast["age_months"] = 0.0   # the generator has no age effect
    rec = {}
    for key in ("coefficients_fit_at_cutoff", "coefficients_fit_at_as_of"):
        rows = []
        for c in res["model"][key]:
            if c["term"] in planted_contrast:
                pv = planted_contrast[c["term"]]
                rows.append({"term": c["term"], "planted": round(pv, 4), "estimate": round(c["estimate"], 4),
                             "ci_low": round(c["ci_low"], 4), "ci_high": round(c["ci_high"], 4),
                             "covered": bool(c["ci_low"] <= pv <= c["ci_high"])})
        est = np.array([r["estimate"] for r in rows])
        pl = np.array([r["planted"] for r in rows])
        rec[key] = {"rows": rows, "n_terms": len(rows), "ci95_coverage": f"{sum(r['covered'] for r in rows)} of {len(rows)}",
                    "corr_estimate_planted": round(float(np.corrcoef(est, pl)[0, 1]), 4),
                    "mean_abs_error": round(float(np.mean(np.abs(est - pl))), 4)}

    # 2. how good could any model be? metrics of the planted probabilities on the same held-out appeals
    cutoff = pd.Timestamp(res["validation_cutoff"])
    ep_all = prioritize.denial_episodes(conn, res["as_of_date"])
    test = ep_all[(ep_all["denial_date"] >= cutoff) & ep_all["appeal_outcome"].notna()].reset_index(drop=True)
    y = (test["appeal_outcome"] == "OVERTURNED").astype(int).to_numpy()
    p_true = true_overturn_p(test, planted, ct)
    ceiling = prioritize.classification_metrics(y, p_true)

    # 3. score each backtest policy's choices with the planted probabilities
    sel = _frame(conn, "select pool, policy, hours_per_day, claim_id, denial_seq from mart.work_queue_backtest_selection")
    pool = ep_all[ep_all["denial_date"] >= cutoff].copy()
    pool["p_true"] = true_overturn_p(pool, planted, ct)
    ratio = prioritize.payer_paid_ratio(conn, cutoff.date())
    h = prioritize.handling(cfg)
    pool["recoverable"] = pool["contract_allowed"] * pool["payer_id"].map(ratio)
    pool["minutes"] = np.rint(h["base_minutes"] * pool["reason_category"].map(h["multiplier"])).astype(int)
    pool["true_value"] = pool["p_true"] * pool["recoverable"] - pool["minutes"] * h["cost_per_minute"]
    m = sel[sel["pool"] == "ALL_DENIALS"].merge(pool[["claim_id", "denial_seq", "true_value"]], on=["claim_id", "denial_seq"])
    tv = m.groupby(["hours_per_day", "policy"])["true_value"].sum().unstack()
    true_val = [{"hours_per_day": float(hh), **{k.lower(): round(float(v), 2) for k, v in r.items()},
                 "knapsack_minus_oldest": round(float(r["MODEL_KNAPSACK"] - r["OLDEST_FIRST"]), 2),
                 "knapsack_minus_largest": round(float(r["MODEL_KNAPSACK"] - r["LARGEST_FIRST"]), 2)}
                for hh, r in tv.iterrows()]
    # an oracle knapsack that knows the planted probabilities, as an upper reference
    oracle = []
    for hh in prioritize.BUDGET_GRID_HOURS:
        sc = pool.copy()
        sc["handling_minutes"] = sc["minutes"]
        sc["expected_value_raw"] = sc["true_value"]
        sc["billed"] = sc["billed"].astype(float)
        tot, _ = prioritize.replay(sc, "MODEL_KNAPSACK", hh, cutoff, pd.Timestamp(res["as_of_date"]),
                                   int(prioritize.APPEAL_WINDOW["value"]))
        oracle.append({"hours_per_day": hh, "oracle_knapsack_true_value": round(tot["expected_net"], 2)})

    return {
        "name": "prioritize_evaluation",
        "label": ME,
        "note": ("Compares the Q2 model with the parameters planted in the simulator (data/truth). Evaluation only: "
                 "shows parameter recovery and decision quality on simulated data, not facts about real payers."),
        "seed": cfg["seed"], "as_of_date": res["as_of_date"], "validation_cutoff": res["validation_cutoff"],
        "parameter_recovery": rec,
        "planted_probability_metrics_on_test": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in ceiling.items()},
        "model_metrics_on_test": res["validation"]["held_out_metrics"]["logistic_model"],
        "backtest_value_under_planted_probabilities": true_val,
        "oracle_knapsack": oracle,
    }


# ------------------------------------------------------------------------------------------
# Q4 evaluation
# ------------------------------------------------------------------------------------------
def evaluate_reserve(conn, res: dict, cfg: dict) -> dict:
    lt = pd.read_csv(TRUTH / "claim_line_truth.csv",
                     usecols=["claim_id", "asof_paid_cents", "asof_paid_visible_cents", "ultimate_paid_cents"])
    ct = pd.read_csv(TRUTH / "claim_truth.csv", usecols=["claim_id", "service_date"])
    lt = lt.merge(ct, on="claim_id")
    lt["m"] = lt["service_date"].str[:7]
    g = lt.groupby("m")[["asof_paid_cents", "asof_paid_visible_cents", "ultimate_paid_cents"]].sum() / 100.0
    g["true_unpaid"] = g["ultimate_paid_cents"] - g["asof_paid_cents"]
    by = pd.DataFrame(res["chain_ladder"]["by_accident_month"]).set_index("accident_month")
    by = by.join(g["true_unpaid"])
    total_true = float(g["true_unpaid"].sum())
    per_claim = ((lt["ultimate_paid_cents"] - lt["asof_paid_cents"]) / 100.0).groupby(lt["claim_id"]).sum()
    per_claim = per_claim[per_claim != 0].sort_values(ascending=False)
    concentration = {"claims_with_unpaid": int(len(per_claim)),
                     "top10_claims_share": round(float(per_claim.head(10).sum() / per_claim.sum()), 4),
                     "largest_claim_unpaid": round(float(per_claim.iloc[0]), 2) if len(per_claim) else 0.0,
                     "note": "the true unpaid amount is one random outcome concentrated in a few large claims, "
                             "so single-month comparisons are noisy"}
    withheld_share = float(1 - g["asof_paid_visible_cents"].sum() / g["asof_paid_cents"].sum())
    cl_total = res["chain_ladder"]["total_unpaid"]["value"]
    bf_total = res["chain_ladder"]["bf_cape_cod_unpaid"]["value"]
    fs_total = res["frequency_severity"]["unpaid_freq_sev_gamma"]["value"]
    fs_ln_total = res["frequency_severity"]["unpaid_freq_sev_lognormal"]["value"]
    bs = res["chain_ladder"]["bootstrap"]
    rows = []
    for mth, r in by.iterrows():
        if r["latest_lag"] > 12:
            continue
        rows.append({"accident_month": mth, "true_unpaid": round(float(r["true_unpaid"]), 2),
                     "unpaid_cl": r["unpaid_cl"], "unpaid_bf": r["unpaid_bf"], "unpaid_freq_sev": r["unpaid_freq_sev"],
                     "boot_p05": r["boot_p05"], "boot_p95": r["boot_p95"],
                     "inside_90pct": bool(r["boot_p05"] <= r["true_unpaid"] <= r["boot_p95"])})
    _fig_validation(rows, FIGURES_DIR / "reserve_validation.png")

    # retrospective: what was still unpaid even at the official as-of for the retro accident months
    retro = []
    for r in res["retrospective"]["rows"]:
        mth = pd.Timestamp(r["retro_as_of"]).strftime("%Y-%m")
        still = float(g.loc[g.index <= mth, "true_unpaid"].sum())
        retro.append({"retro_as_of": r["retro_as_of"], "estimate_cl": r["unpaid_cl"], "estimate_bf": r["unpaid_bf"],
                      "paid_later_observed": r["paid_later_observed"], "still_unpaid_at_official_as_of": round(still, 2),
                      "observed_plus_still_unpaid": round(r["paid_later_observed"] + still, 2)})
    return {
        "name": "reserve_evaluation",
        "label": ME,
        "note": ("Compares the Q4 reserve with the unpaid amount the simulator knows (ultimate paid minus paid by the "
                 "as-of date, data/truth). Evaluation only; simulated data."),
        "seed": cfg["seed"], "as_of_date": res["as_of_date"],
        "true_unpaid_total": labels.stamp(round(total_true, 2), SR, note="sum over lines of ultimate paid - paid by as-of"),
        "estimates": {
            "chain_ladder": {"value": cl_total, "error_pct": round(100 * (cl_total / total_true - 1), 2)},
            "bornhuetter_ferguson_cape_cod": {"value": bf_total, "error_pct": round(100 * (bf_total / total_true - 1), 2)},
            "freq_sev_gamma_means": {"value": fs_total, "error_pct": round(100 * (fs_total / total_true - 1), 2)},
            "freq_sev_lognormal_means": {"value": fs_ln_total, "error_pct": round(100 * (fs_ln_total / total_true - 1), 2)},
            "bootstrap_p05_p95": [bs["p05"]["value"], bs["p95"]["value"]],
            "true_inside_bootstrap_90pct": bool(bs["p05"]["value"] <= total_true <= bs["p95"]["value"]),
            "mack_se": res["chain_ladder"]["mack_se_total"]["value"],
            "error_in_mack_se_units": round((cl_total - total_true) / res["chain_ladder"]["mack_se_total"]["value"], 3),
        },
        "true_unpaid_concentration": concentration,
        "remittance_visibility": {
            "withheld_share_of_paid": round(withheld_share, 4),
            "note": ("Some payments reach the bank without a visible remittance line (a planted defect). The triangle can "
                     "only develop what is visible, so its natural target is slightly below the true unpaid amount."),
        },
        "by_accident_month_last_13": rows,
        "accident_months_inside_90pct": f"{sum(r['inside_90pct'] for r in rows)} of {len(rows)}",
        "retrospective": retro,
        "figure": "figures/reserve_validation.png",
    }


def _fig_validation(rows, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    x = np.arange(len(rows))
    fig, ax = plt.subplots(figsize=(8, 4.4))
    cl = np.array([r["unpaid_cl"] for r in rows]) / 1e3
    lo = np.array([r["boot_p05"] for r in rows]) / 1e3
    hi = np.array([r["boot_p95"] for r in rows]) / 1e3
    ax.errorbar(x - 0.15, cl, yerr=[np.clip(cl - lo, 0, None), np.clip(hi - cl, 0, None)], fmt="o", color="0.1",
                capsize=2, label="chain-ladder (bootstrap 5-95%)")
    ax.plot(x, [r["unpaid_bf"] / 1e3 for r in rows], "^", color="0.4", label="Bornhuetter-Ferguson")
    ax.plot(x + 0.15, [r["true_unpaid"] / 1e3 for r in rows], "s", color="0.65", ms=7, label="true unpaid (simulator)")
    ax.set_xticks(x)
    ax.set_xticklabels([r["accident_month"] for r in rows], rotation=45, fontsize=8)
    ax.set_xlabel("Service month")
    ax.set_ylabel("Unpaid at 2026-03-31 ($ thousand)")
    ax.set_title("Reserve by service month vs the simulator's true unpaid amount", fontsize=10)
    ax.legend(fontsize=8, frameon=False)
    ax.grid(alpha=0.25)
    fig.text(0.01, 0.01, "label: model_estimate vs synthetic_record (evaluation only)", fontsize=7, color="0.4")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=("prioritize", "reserve"), default=None)
    ap.add_argument("--no-eval", action="store_true", help="skip the comparison with data/truth")
    args = ap.parse_args(argv)
    cfg = model_config()
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = {}
    with db.connect() as conn:
        for name, mod, ev in (("prioritize", prioritize, evaluate_prioritize), ("reserve", reserve, evaluate_reserve)):
            if args.only and args.only != name:
                continue
            t = time.time()
            res = mod.run(conn, cfg)
            conn.commit()
            res["runtime_seconds"] = round(time.time() - t, 2)
            (RESULTS_DIR / f"{name}.json").write_text(json.dumps(_jsonable(res), indent=2, default=str), encoding="utf-8")
            out[name] = res["runtime_seconds"]
            if not args.no_eval:
                e = ev(conn, _jsonable(res), cfg)
                (RESULTS_DIR / f"{name}_evaluation.json").write_text(json.dumps(_jsonable(e), indent=2, default=str),
                                                                     encoding="utf-8")
            print(f"[run_models_ops] {name}: {res['runtime_seconds']}s -> results/{name}.json", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
