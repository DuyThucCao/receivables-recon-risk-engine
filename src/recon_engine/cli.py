"""Command-line entry points.

    python run_all.py                     # generate -> build -> close days -> evaluate -> models -> summary
    python -m recon_engine.cli generate   # raw files and ground truth only
    python -m recon_engine.cli build      # rebuild the database from data/raw
    python -m recon_engine.cli evaluate   # score reconciliation against ground truth
    python -m recon_engine.cli report     # reports/cfo_report.pdf (run_all also builds it)
    python -m recon_engine.cli close-day 2026-03-31
    python -m recon_engine.cli ingest --remittance new_835.csv --bank new_statement.csv

``run_all`` is the orchestrator: it is the one place that both calls the generator (which writes
ground truth) and the evaluation (which reads it). The pipeline itself never touches ground truth.
"""
from __future__ import annotations

import argparse
import csv
import importlib
import json
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

from . import RAW_DIR, RESULTS_DIR, SAMPLES_DIR, labels
from . import db, pipeline

SAMPLE_ROWS = 50


def write_samples(raw_dir: Path = RAW_DIR, out_dir: Path = SAMPLES_DIR, n: int = SAMPLE_ROWS) -> None:
    """Copy the header and first ``n`` rows of each raw file to data/samples/ for readers."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in pipeline.RAW_TABLES:
        src = raw_dir / name
        with open(src, newline="", encoding="utf-8") as f, open(out_dir / name, "w", newline="", encoding="utf-8") as g:
            r, w = csv.reader(f), csv.writer(g, lineterminator="\n")
            for i, row in enumerate(r):
                if i > n:
                    break
                w.writerow(row)


_REPO_ROOT = str(RESULTS_DIR.resolve().parent) + "/"


def _jsonable(x: Any) -> Any:
    """Make ``x`` JSON-ready. Paths inside the repository are written relative to its root, so
    committed results never carry a machine-specific absolute path."""
    if isinstance(x, Path):
        x = str(x)
    if isinstance(x, str):
        return x[len(_REPO_ROOT):] if x.startswith(_REPO_ROOT) else x
    if isinstance(x, dict):
        return {k: _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, (date,)):
        return x.isoformat()
    try:
        import decimal
        if isinstance(x, decimal.Decimal):
            return float(x)
    except Exception:  # pragma: no cover
        pass
    return x


def write_json(path: Path, obj: Any) -> None:
    """Write JSON atomically (temp file + rename) so a reader never sees a half-written file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def snapshot_core_results(conn, build_info: dict, overrides: dict, results_dir: Path = RESULTS_DIR) -> dict:
    """Write results/core_build.json: row counts, timings, close result, exceptions, Q1 marts."""
    q = lambda s: conn.execute(s).fetchall()  # noqa: E731
    out = {
        "label": labels.SYNTHETIC_RECORD,
        "note": "Build of the reconciliation core on simulated data for a fictional hospital.",
        "as_of_date": conn.execute("select recon.param('as_of_date')").fetchone()[0],
        "build": build_info,
        "calibration": overrides,
        "row_counts": {t: conn.execute(f"select count(*) from {t}").fetchone()[0] for t in (
            "core.fact_claim_line", "core.fact_remit_line", "core.fact_bank_txn", "core.fact_claim_event",
            "core.fact_drug_purchase", "core.fact_drug_dispense", "core.dim_contract_rate", "stg.reject",
            "recon.claim_remit_match", "recon.remit_bank_match", "recon.exception_queue")},
        "matches_by_rule": {r[0]: r[1] for r in q(
            "select rule_id, count(*) from recon.claim_remit_match group by 1 union all "
            "select rule_id, count(*) from recon.remit_bank_match group by 1 union all "
            "select rule_id, count(*) from recon.remit_duplicate group by 1 order by 1")},
        "open_exceptions": [{"type": r[0], "severity": r[1], "n": r[2], "amount": r[3]} for r in q(
            "select exception_type, severity, count(*), coalesce(sum(amount), 0) from recon.exception_queue "
            "where status = 'OPEN' group by 1, 2 order by 1")],
        "control_totals_passed": conn.execute("select bool_and(passed) from recon.control_totals").fetchone()[0],
        "days_closed": conn.execute("select count(*) from recon.daily_recon where status = 'CLOSED'").fetchone()[0],
        "days_total": conn.execute("select count(*) from recon.daily_recon").fetchone()[0],
        "cash_stuck": [{"bucket": r[0], "n_items": r[1], "amount": r[2]} for r in q(
            "select bucket, n_items, amount from mart.cash_stuck order by sort")],
        "claim_line_status": {r[0]: r[1] for r in q("select status, count(*) from recon.claim_line_status group by 1 order by 1")},
    }
    out = _jsonable(out)
    results_dir.mkdir(parents=True, exist_ok=True)
    write_json(results_dir / "core_build.json", out)
    ct = [dict(zip(("check_name", "scope", "expected", "actual", "difference", "passed", "detail"), r)) for r in q(
        "select check_name, scope, expected, actual, difference, passed, detail from recon.control_totals order by check_id")]
    write_json(results_dir / "control_totals.json",
               {"label": labels.SYNTHETIC_RECORD, "note": "Control totals of the latest reconciliation run, to the cent.",
                "checks": _jsonable(ct)})
    return out


def run_models(conn, cfg: dict, results_dir: Path = RESULTS_DIR) -> dict[str, Any]:
    from .models import REGISTRY
    out = {}
    for mod_name in REGISTRY:
        name = mod_name.rsplit(".", 1)[-1]
        t = time.time()
        try:
            mod = importlib.import_module(mod_name)
            print(f"[run_all]   model {name} ...", flush=True)
            res = mod.run(conn, cfg)
            conn.commit()
            res["seconds"] = round(time.time() - t, 1)
            write_json(results_dir / f"{name}.json", _jsonable(res))
            out[name] = {"status": "ok", "seconds": round(time.time() - t, 2)}
        except Exception as e:  # keep going; record the failure honestly
            conn.rollback()
            out[name] = {"status": "failed", "error": f"{type(e).__name__}: {e}"}
    return out


def _fmt_money(x: Any) -> str:
    try:
        return f"{float(x):,.2f}"
    except (TypeError, ValueError):
        return str(x)


def write_summary(results_dir: Path = RESULTS_DIR, model_status: dict | None = None,
                  eval_status: dict | None = None) -> Path:
    """Generate results/summary.md from the JSON files in results/. Never hand-edited."""
    lines = ["# Results summary", "",
             "_Generated by `run_all.py` from the JSON files in `results/`. Do not edit by hand._", "",
             "All figures come from simulated data for a fictional hospital (label `synthetic_record` unless stated). "
             "They demonstrate the method; they are not findings about real payers, banks or hospitals.", ""]
    core = results_dir / "core_build.json"
    if core.exists():
        c = json.loads(core.read_text())
        lines += ["## Reconciliation core", "",
                  f"- As-of date: {c['as_of_date']}",
                  f"- Control totals all passed: **{c['control_totals_passed']}**",
                  f"- Days closed by `recon.close_day`: {c['days_closed']} of {c['days_total']}",
                  f"- Build time: {c['build'].get('timings_s', {}).get('total')} s (database build and close only)", "",
                  "| Where cash is stuck | Items | Amount |", "|---|---:|---:|"]
        for r in c["cash_stuck"]:
            lines.append(f"| {r['bucket']} | {r['n_items']} | {_fmt_money(r['amount'])} |")
        lines.append("")
        if c["calibration"].get("overrides"):
            lines += ["Public figures used in place of assumptions:", ""]
            for o in c["calibration"]["overrides"]:
                lines.append(f"- `{o['parameter']}` ({o['scope']}): {o['applied_value']} [{o['source_class']}]")
            lines.append("")
        else:
            lines += ["No public calibration file was found; every generator parameter is an assumption.", ""]
    rec = results_dir / "reconciliation.json"
    if rec.exists():
        r = json.loads(rec.read_text())
        cr, rb = r["claim_to_remittance"], r["remittance_to_bank"]
        lines += ["## Matching against planted ground truth", "",
                  "Counts over simulated records scored against the generator's ground truth. The generator and the "
                  "rules were written by the same project, so these numbers check internal consistency on the planted "
                  "cases; they are not evidence of real-world matching accuracy.", "",
                  f"- Tie-out: {r['tie_out']['checks']} control totals, all passed = {r['tie_out']['all_control_totals_passed']}, "
                  f"largest difference {r['tie_out']['max_abs_difference']:.2f}",
                  f"- Claim to remittance: precision {cr['overall']['precision']}, recall {cr['overall']['recall_on_postable_lines']}, "
                  f"exception-queue share {cr['overall']['exception_queue_share_of_postable']} ({cr['overall']['matched']} matched)",
                  f"- Remittance to bank: precision {rb['overall']['precision']}, recall {rb['overall']['recall']}, "
                  f"exception-queue share {rb['overall']['exception_queue_share']} ({rb['overall']['matched']} payments matched)",
                  f"- Duplicate lines: precision {cr['duplicate_detection']['precision']}, recall {cr['duplicate_detection']['recall']}", "",
                  "| Rule | Matched | Wrong | Precision |", "|---|---:|---:|---:|"]
        for k, v in cr["per_rule"].items():
            lines.append(f"| {k} | {v['matched']} | {v['wrong']} | {v['precision']} |")
        for k, v in rb["per_rule"].items():
            lines.append(f"| {k} | {v['payments_matched']} | {v['wrong']} | {v['precision']} |")
        lines += ["", "| Claim-side case | Lines | Correct | Wrong | Queued (ambiguous) | Recall | Queue share |",
                  "|---|---:|---:|---:|---:|---:|---:|"]
        for k, v in cr["by_case"].items():
            lines.append(f"| {k} | {v['lines']} | {v['matched_correct']} | {v['matched_wrong'] + v['wrongly_excluded_as_duplicate']} | "
                         f"{v['sent_to_exception_queue']} ({v['of_which_ambiguous']}) | {v['recall']} | {v['exception_queue_share']} |")
        lines += ["", "| Bank-side case | Payments | Correct | Wrong | Queued (ambiguous) | Recall |", "|---|---:|---:|---:|---:|---:|"]
        for k, v in rb["by_case"].items():
            if "recall" in v:
                lines.append(f"| {k} | {v['payments']} | {v['matched_correct']} | {v['matched_wrong']} | "
                             f"{v['sent_to_exception_queue']} ({v['of_which_ambiguous']}) | {v['recall']} |")
            else:
                lines.append(f"| {k} (should stay unmatched) | {v['payments']} | {v['correctly_left_unmatched']} | {v['wrongly_matched']} | - | - |")
        lines += ["", "What the rules cannot resolve automatically:", ""]
        for x in r.get("what_the_rules_cannot_resolve", []):
            lines.append(f"- **{x['case']}**: {x['why']}")
        lines.append("")
    others = sorted(p for p in results_dir.glob("*.json") if p.name not in ("core_build.json", "reconciliation.json", "control_totals.json"))
    if others or model_status:
        lines += ["## Analysis modules", ""]
        for p in others:
            lines.append(f"- `{p.name}`")
        if model_status or eval_status:
            lines += ["", "| Module | Model run | Seconds | Evaluation against planted parameters |", "|---|---|---:|---|"]
            for k in dict.fromkeys(list(model_status or {}) + list(eval_status or {})):
                m, e = (model_status or {}).get(k, {}), (eval_status or {}).get(k, {})
                lines.append(f"| {k} | {m.get('status', '-')}{' (' + m['error'] + ')' if m.get('error') else ''} | "
                             f"{m.get('seconds', '-')} | {e.get('status', '-')}{' (' + e['error'] + ')' if e.get('error') else ''} |")
        lines.append("")
    else:
        lines += ["## Analysis modules", "", "No analysis module is registered yet (`recon_engine.models.REGISTRY` is empty).", ""]
    out = results_dir / "summary.md"
    tmp = out.with_name(".summary.md.tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8")
    tmp.replace(out)
    return out


def run_all(args: argparse.Namespace) -> dict:
    from .datagen import generate
    from .datagen.calibration import load_calibration, model_config, override_record
    from .datagen.evaluate import run_evaluation

    t0 = time.time()
    cal = load_calibration()
    cfg = model_config(cal)
    if not args.skip_generate:
        print("[run_all] generating raw documents ...", flush=True)
        generate(cal=cal, n_claims=args.n_claims)
        write_samples()
    t_gen = round(time.time() - t0, 2)
    print("[run_all] building database ...", flush=True)
    info = pipeline.build(RAW_DIR, run_params={"as_of_date": cfg["as_of_date"]})
    info["generate_seconds"] = t_gen
    with db.connect() as conn:
        snapshot_core_results(conn, info, override_record(cal))
        print("[run_all] evaluating against ground truth ...", flush=True)
        run_evaluation(conn)
        print("[run_all] running registered models ...", flush=True)
        status = run_models(conn, cfg)
        eval_status = {}
        if not args.no_eval:
            print("[run_all] evaluating models against planted parameters ...", flush=True)
            eval_status = _evaluate_models(conn, cfg)
    write_summary(model_status=status, eval_status=eval_status)
    try:
        from .reports.cfo_report import build as build_cfo_report
        print(f"[run_all] wrote {build_cfo_report()}", flush=True)
    except Exception as e:  # the report is a by-product; never hide a failure, never stop the build for it
        print(f"[run_all] CFO report not built: {type(e).__name__}: {e}", flush=True)
    total = round(time.time() - t0, 2)
    core = json.loads((RESULTS_DIR / "core_build.json").read_text())
    core["build"]["run_all_total_seconds"] = total
    write_json(RESULTS_DIR / "core_build.json", core)
    print(f"[run_all] done in {total}s; see results/summary.md", flush=True)
    return {"seconds": total, "models": status, "evaluation": eval_status}


def _evaluate_models(conn, cfg: dict) -> dict:
    """Run scripts/evaluate_models.py (evaluation code lives outside the package because it reads ground truth)."""
    import importlib.util
    path = Path(__file__).resolve().parents[2] / "scripts" / "evaluate_models.py"
    spec = importlib.util.spec_from_file_location("_rre_evaluate_models", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.evaluate_all(conn, cfg)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="recon_engine")
    sub = p.add_subparsers(dest="cmd")
    a = sub.add_parser("all", help="generate, build, close, evaluate, run models, summarise")
    a.add_argument("--skip-generate", action="store_true", help="reuse data/raw")
    a.add_argument("--n-claims", type=int, default=None, help="override the claim count (smaller = faster)")
    a.add_argument("--no-eval", action="store_true", help="skip comparing model outputs with planted parameters")
    g = sub.add_parser("generate")
    g.add_argument("--n-claims", type=int, default=None)
    sub.add_parser("build")
    sub.add_parser("evaluate")
    sub.add_parser("report", help="build reports/cfo_report.pdf from the database and results/")
    c = sub.add_parser("close-day")
    c.add_argument("date")
    i = sub.add_parser("ingest")
    i.add_argument("--remittance")
    i.add_argument("--bank")
    i.add_argument("--claim", help="CSV with claims_export columns")
    args = p.parse_args(argv)
    if args.cmd in (None, "all"):
        if args.cmd is None:
            args = p.parse_args(["all"] + (argv or []))
        run_all(args)
    elif args.cmd == "generate":
        from .datagen import generate
        print(json.dumps(generate(n_claims=args.n_claims)["files"], indent=2))
    elif args.cmd == "build":
        from .datagen.calibration import model_config
        print(json.dumps(pipeline.build(run_params={"as_of_date": model_config()["as_of_date"]})["timings_s"], indent=2))
    elif args.cmd == "evaluate":
        from .datagen.evaluate import run_evaluation
        with db.connect() as conn:
            r = run_evaluation(conn)
        print(json.dumps({"claim_remit": r["claim_to_remittance"]["overall"], "remit_bank": r["remittance_to_bank"]["overall"]}, indent=2))
    elif args.cmd == "report":
        from .reports.cfo_report import build as build_cfo_report
        print(build_cfo_report())
    elif args.cmd == "close-day":
        with db.connect() as conn:
            conn.execute("call recon.close_day(%s)", (args.date,))
            conn.commit()
        print(f"closed {args.date}")
    elif args.cmd == "ingest":
        r = pipeline.ingest_and_reconcile(remittance=args.remittance, bank_statement=args.bank, claim=args.claim)
        print(json.dumps({"landed": r["landed"], "new_exceptions": len(r["new_exceptions"]), "close": r["close"]}, indent=2, default=str))
        print(r["new_exceptions"].to_string())
    return 0


if __name__ == "__main__":
    sys.exit(main())
