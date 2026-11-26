#!/usr/bin/env python
"""Run the chain, cash and credit models (shared Markov chain, Q3 cash forecast, Q5 borrowing base)
on the built database and evaluate them against the generator's planted parameters.

    export RRE_PG_DIR=~/.cache/receivables-engine/pg-risk
    python run_all.py --skip-generate      # build the database first
    python scripts/run_models_risk.py      # add --only markov,credit to subset

Writes
    results/markov.json, results/cashflow.json, results/credit.json     model outputs (no ground truth used)
    results/markov_validation.json, results/cashflow_validation.json, results/credit_validation.json
                                                                         comparison with the generator records
    figures/markov_*.png, figures/cashflow_*.png, figures/credit_*.png

This evaluation script is the only place in the Q3/Q5 area that opens the generator's records
(data/truth). The model modules never do.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recon_engine import RESULTS_DIR, db, labels  # noqa: E402
from recon_engine.cli import _jsonable  # noqa: E402
from recon_engine.datagen.calibration import model_config  # noqa: E402
from recon_engine.models import cashflow, credit, markov  # noqa: E402

TRUTH = ROOT / "data" / "truth"
ME = labels.MODEL_ESTIMATE


def write(name: str, obj: dict) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / f"{name}.json").write_text(json.dumps(_jsonable(obj), indent=2, default=str), encoding="utf-8")
    print(f"[risk] wrote results/{name}.json", flush=True)


def validate_markov(conn, cfg: dict, planted: dict) -> dict:
    as_of = cfg["as_of_date"]
    out = {"label_note": "Comparison of estimates with parameters planted in the simulation (evaluation only).",
           "tolerance": "each cell |error| / sqrt(p(1-p)/n_row) <= 4 (about a Bonferroni bound over 35 cells x 6 payers); "
                        "planted P(PAID) inside the 95% claim-level bootstrap interval for most payers"}
    for mat in (180, 0):
        ch = markov.fit(conn, as_of, maturity_days=mat, n_boot=500, seed=int(cfg["seed"]))
        ev = markov.evaluate_against_planted(ch, planted)
        out[f"maturity_{mat}_days"] = labels.stamp(ev, ME)
        if mat == 180:
            out["figure"] = markov.recovery_figure(ev, ch, planted)
    return out


def validate_cashflow(conn, planted: dict) -> dict:
    ev = cashflow.evaluate_against_planted(conn, planted)
    return {"label_note": "Weighted Kaplan-Meier quantiles of the first-pass payment lag (submission to payment, "
                          "no denial or pend) against the generator's realised quantiles over all claims, including "
                          "payments after the as-of date that the model cannot see.",
            "tolerance": "quartiles within 3 days at the as-of cutoff",
            "first_pass_lag": labels.stamp(ev, ME)}


def validate_credit(conn, cfg: dict) -> dict:
    ct = pd.read_csv(TRUTH / "claim_truth.csv", usecols=["claim_id", "final_state_ultimate"])
    prm = credit._params(cfg)
    cert = credit.certificate(conn, prm["steady_state_date"], cfg, prm, seed=int(cfg["seed"]))
    return {"label_note": "Predicted write-off probability of the eligible pool at the steady-state certificate against "
                          "the generator's ultimate outcome per claim (includes outcomes after the as-of date).",
            "pd_vs_ultimate_outcome": labels.stamp(credit.evaluate_against_planted(cert, ct), ME),
            "note": "p_written_off in the eligible pool comes from the dollar-weighted chain, so the NCV-weighted "
                    "figures are the ones to compare; count-weighted figures are shown for completeness."}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="markov,cashflow,credit")
    args = ap.parse_args(argv)
    only = set(args.only.split(","))
    cfg = model_config()
    planted = json.loads((TRUTH / "planted_parameters.json").read_text())
    with db.connect() as conn:
        for name, mod in (("markov", markov), ("cashflow", cashflow), ("credit", credit)):
            if name not in only:
                continue
            t = time.time()
            res = mod.run(conn, cfg)
            conn.commit()
            res["seconds"] = round(time.time() - t, 1)
            write(name, res)
        if "markov" in only:
            write("markov_validation", validate_markov(conn, cfg, planted))
        if "cashflow" in only:
            write("cashflow_validation", validate_cashflow(conn, planted))
        if "credit" in only:
            write("credit_validation", validate_credit(conn, cfg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
