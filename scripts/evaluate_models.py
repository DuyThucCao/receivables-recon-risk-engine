#!/usr/bin/env python
"""Compare every registered model's output with the parameters planted by the generator.

    python scripts/evaluate_models.py        # after run_all.py

Reads the model outputs already written to results/<name>.json (it does not re-run the models)
and writes results/<name>_validation.json or results/<name>_evaluation.json. Like the three
area scripts it calls, this is an evaluation step: it may open data/truth/, the model modules
never do. ``run_all.py`` calls ``evaluate_all`` after the models have run.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recon_engine import RESULTS_DIR, db  # noqa: E402
from recon_engine.cli import _jsonable, write_json  # noqa: E402
from recon_engine.datagen.calibration import model_config  # noqa: E402

TRUTH = ROOT / "data" / "truth"


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(f"_rre_{name}", ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _result(name: str) -> dict | None:
    p = RESULTS_DIR / f"{name}.json"
    return json.loads(p.read_text()) if p.exists() else None


def evaluate_all(conn, cfg: dict | None = None) -> dict:
    cfg = cfg or model_config()
    planted = json.loads((TRUTH / "planted_parameters.json").read_text())
    risk, ops, econ = (_load_script(n) for n in ("run_models_risk", "run_models_ops", "run_models_econ"))

    def markov():
        return {"markov_validation": risk.validate_markov(conn, cfg, planted)}

    def cashflow():
        return {"cashflow_validation": risk.validate_cashflow(conn, planted)}

    def credit():
        return {"credit_validation": risk.validate_credit(conn, cfg)}

    def prioritize():
        return {"prioritize_evaluation": ops.evaluate_prioritize(conn, _result("prioritize"), cfg)}

    def reserve():
        return {"reserve_evaluation": ops.evaluate_reserve(conn, _result("reserve"), cfg)}

    def drug():
        return {"drug_validation": econ.validate_drug(conn)}

    def econ_():
        ve, per_payer = econ.validate_econ(conn, _result("econ"))
        ve["figures"] = [econ.validation_figure(per_payer)]
        return {"econ_validation": ve}

    steps = {"markov": markov, "cashflow": cashflow, "credit": credit, "prioritize": prioritize,
             "reserve": reserve, "drug": drug, "econ": econ_}
    status = {}
    for model, fn in steps.items():
        if _result(model) is None:
            status[model] = {"status": "skipped", "error": f"results/{model}.json not found"}
            continue
        t = time.time()
        try:
            for out_name, obj in fn().items():
                write_json(RESULTS_DIR / f"{out_name}.json", _jsonable(obj))
            conn.commit()
            status[model] = {"status": "ok", "seconds": round(time.time() - t, 2)}
        except Exception as e:  # record the failure honestly and keep going
            conn.rollback()
            status[model] = {"status": "failed", "error": f"{type(e).__name__}: {e}"}
        print(f"[evaluate] {model}: {status[model]}", flush=True)
    return status


def main() -> int:
    with db.connect() as conn:
        status = evaluate_all(conn)
    print(json.dumps(status, indent=2))
    return 0 if all(v["status"] == "ok" for v in status.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
