#!/usr/bin/env python
"""Collect every number the documents quote into one generated table, and refresh the documents.

Documents never type a result by hand. They carry inline markers that GitHub does not render:

    precision <!--n:recon.c2r.precision-->1.0000<!--/n-->

This script reads ``results/*.json`` (plus a few facts about the repository itself, such as the
number of tests), formats each registered number, and

* writes ``docs/_numbers.md`` (key, value, data label, source file, meaning) and
  ``docs/_numbers.json`` (the same, machine-readable, used by ``scripts/build_dossier.py``);
* with ``--apply``, rewrites the text between every marker in the documentation files listed in
  ``DOC_GLOBS`` with the current value (the drafting shorthand ``{{n:key}}`` is expanded into a
  full marker first). A number whose results file or field does not exist yet
  is written as ``{{key}}`` so a missing input is visible instead of silently stale;
* with ``--check``, changes nothing and exits 1 if any marker is stale, uses an unknown key or
  still shows a placeholder.

Usage (from the repository root):

    python scripts/build_docs_numbers.py            # table only
    python scripts/build_docs_numbers.py --apply    # table + docs
    python scripts/build_docs_numbers.py --check    # CI-style check

Run it after every ``run_all.py``. It only reads results and never touches the database.
"""
from __future__ import annotations

import argparse
import glob
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "results"
OUT_MD = ROOT / "docs" / "_numbers.md"
OUT_JSON = ROOT / "docs" / "_numbers.json"

DOC_GLOBS = [
    "README.md",
    "INTERVIEW_GUIDE.md",
    "docs/one_pagers/*.md",
    "docs/career/*.md",
    "docs/project_dossier/src/*.md",
    "practice/**/*.md",
    "docs/consulting_memo.md",
]
MARKER = re.compile(r"<!--n:([A-Za-z0-9_.\-]+)-->(.*?)<!--/n-->", re.S)
# authoring shorthand: write {{n:key}} while drafting; --apply turns it into a full marker
SHORT = re.compile(r"\{\{n:([A-Za-z0-9_.\-]+)\}\}")
CODE_FACT = "n/a (code fact)"


class Missing(Exception):
    pass


# ------------------------------------------------------------------------------------------
# reading
# ------------------------------------------------------------------------------------------
_cache: dict[str, Any] = {}


def load(name: str) -> Any:
    """Read results/<name> (JSON), or a generated public-calibration file given as config/<file>.yaml."""
    if name not in _cache:
        if name.endswith(".yaml"):
            import yaml  # noqa: PLC0415

            p = ROOT / name
            if not p.exists():
                raise Missing(f"{name} not found")
            _cache[name] = yaml.safe_load(p.read_text())
        else:
            p = RESULTS / name
            if not p.exists():
                raise Missing(f"results/{name} not found")
            _cache[name] = json.loads(p.read_text())
    return _cache[name]


_SEG = re.compile(r"([^.\[\]]+)|\[(\d+)\]|\[([^=\]]+)=([^\]]+)\]")


def walk(obj: Any, path: str) -> tuple[Any, list[Any]]:
    """Follow ``a.b[2].c`` or ``rows[payer_id=P03].x``; return the value and its ancestors."""
    chain: list[Any] = []
    cur = obj
    for m in _SEG.finditer(path):
        chain.append(cur)
        key, idx, fk, fv = m.groups()
        try:
            if key is not None:
                cur = cur[key]
            elif idx is not None:
                cur = cur[int(idx)]
            else:
                hits = [r for r in cur if isinstance(r, dict) and str(r.get(fk)) == fv]
                if not hits:
                    raise KeyError(f"{fk}={fv}")
                cur = hits[0]
        except (KeyError, IndexError, TypeError) as e:
            raise Missing(f"field '{path}' not found ({e})") from None
    return cur, chain


def auto_label(path: str, chain: list[Any], default: str) -> str:
    last = path.split(".")[-1]
    if last == "value" and isinstance(chain[-1], dict) and isinstance(chain[-1].get("label"), str):
        return chain[-1]["label"]
    for anc in reversed(chain):
        if isinstance(anc, dict) and isinstance(anc.get("label"), str):
            return anc["label"]
    return default


# ------------------------------------------------------------------------------------------
# formatting
# ------------------------------------------------------------------------------------------
def _money(v: float, nd: int) -> str:
    s = f"{abs(v):,.{nd}f}"
    return f"-${s}" if v < 0 else f"${s}"


def fmt(v: Any, how: str) -> str:
    if how == "str":
        return str(v)
    if how == "list":
        return ", ".join(str(x) for x in v)
    if how == "yesno":
        return "yes" if v else "no"
    if how == "passed":
        return "all passed" if v else "NOT all passed"
    if how.startswith("ci_"):
        inner = how[3:]
        return f"{fmt(v[0], inner)} to {fmt(v[1], inner)}"
    v = float(v)
    if how == "int":
        return f"{int(round(v)):,}"
    if how in ("usd0", "usd2"):
        return _money(v, int(how[-1]))
    if how == "usdm1":
        return f"{'-' if v < 0 else ''}${abs(v) / 1e6:,.1f} million"
    if how == "usdm2":
        return f"{'-' if v < 0 else ''}${abs(v) / 1e6:,.2f} million"
    if how == "usdk":
        return f"{'-' if v < 0 else ''}${abs(v) / 1e3:,.0f} thousand"
    if how.startswith("pct"):
        return f"{v * 100:.{int(how[3:])}f}%"
    if how.startswith("f"):
        return f"{v:.{int(how[1:])}f}"
    if how == "g":
        return f"{v:g}"
    raise ValueError(how)


# ------------------------------------------------------------------------------------------
# repository facts (not data; label "n/a (code fact)")
# ------------------------------------------------------------------------------------------
def tests_collected() -> int:
    try:
        out = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
                             cwd=ROOT, capture_output=True, text=True, timeout=300).stdout
        m = re.search(r"(\d+) tests? collected", out)
        if m:
            return int(m.group(1))
    except Exception:  # noqa: BLE001 - fall back to a static count
        pass
    n = 0
    for f in (ROOT / "tests").glob("test_*.py"):
        n += len(re.findall(r"^def test_", f.read_text(), re.M))
    return n


def match_rules() -> int:
    txt = (ROOT / "sql" / "03_reference.sql").read_text()
    ids = set(re.findall(r"'([A-Z]{2,3}\d\d_[A-Z_]+)'", txt))
    if not ids:
        raise Missing("no rule ids found in sql/03_reference.sql")
    return len(ids)


def app_command() -> str:
    launcher = ROOT / "app" / "launch.py"
    if launcher.exists():
        return "python app/launch.py"
    for f in sorted((ROOT / "app").glob("*.py")):
        if "streamlit" in f.read_text(errors="ignore"):
            return f"python app/{f.name}"
    raise Missing("no Streamlit entry point in app/ yet")


def report_pdf() -> str:
    hits = [p for p in ROOT.glob("**/*.pdf")
            if "project_dossier" not in p.parts and ".git" not in p.parts and "data" not in p.parts]
    hits = [p for p in hits if re.search(r"cfo|report", p.name, re.I)]
    if not hits:
        raise Missing("no CFO report PDF found yet")
    return str(sorted(hits)[0].relative_to(ROOT))


def app_screenshots() -> int:
    return len(list((ROOT / "figures").glob("app_*.png")))


# ------------------------------------------------------------------------------------------
# helpers for derived numbers
# ------------------------------------------------------------------------------------------
def rows(file: str, path: str) -> list[dict]:
    v, _ = walk(load(file), path)
    return v


def sum_raw_rows() -> float:
    return float(sum(load("core_build.json")["build"]["raw_rows"].values()))


def check(name: str, scope: str | None = None, field: str = "expected") -> float:
    for c in load("control_totals.json")["checks"]:
        if c["check_name"] == name and (scope is None or c["scope"] == scope):
            return c[field]
    raise Missing(f"control total {name} not found")


def count_label(file: str, path: str, field: str, pred: Callable[[Any], bool]) -> int:
    return sum(1 for r in rows(file, path) if pred(r.get(field)))


def bank_wrong() -> int:
    per = load("reconciliation.json")["remittance_to_bank"]["per_rule"]
    return int(sum(v.get("wrong", 0) for v in per.values()))


def markov_paid_range(which: str) -> float:
    pp = load("markov.json")["per_payer"]
    vals = [pp[p]["p_paid_from_submitted"]["value"] for p in pp if p != "ALL"]
    return min(vals) if which == "min" else max(vals)


def econ_rows(field: str, which: str) -> float:
    vals = [r[field] for r in rows("econ.json", "incentives_by_payer.rows")]
    return min(vals) if which == "min" else max(vals)


def lognormal_best() -> str:
    lm = load("cashflow.json")["lag_model"]["value"]
    n = sum(1 for v in lm.values() if v.get("best_parametric") == "lognormal")
    return f"{n} of {len(lm)}"


def cash_stuck(bucket: str, field: str) -> float:
    for r in load("core_build.json")["cash_stuck"]:
        if r["bucket"] == bucket:
            return r[field]
    raise Missing(bucket)


def gap_reason(reason: str, field: str) -> float:
    return float(sum(r[field] for r in load("econ.json")["gap_reasons"]["rows"] if r["gap_reason"] == reason))


def full_run_minutes() -> float:
    total = load("core_build.json")["build"]["timings_s"]["total"]
    for m in ("markov", "cashflow", "credit", "prioritize", "reserve", "drug", "econ"):
        d = load(f"{m}.json")
        total += d.get("seconds", d.get("runtime_seconds", 0)) or 0
    return total / 60


def drugs_losing_if_patient_pays() -> int:
    return sum(1 for r in load("drug.json")["drugs_mature"]["rows"] if r["margin_if_patient_pays"] < 0)


def n_exception_types() -> int:
    return len({r["exception_type"] for r in load("reconciliation.json")["open_exceptions"]})


# ------------------------------------------------------------------------------------------
# the registry: (key, source, getter, format, meaning[, label])
# getter = JSON path string (read from `source`) or a zero-argument callable
# ------------------------------------------------------------------------------------------
S = "synthetic_record"
M = "model_estimate"
FA = "financial_assumption"
OP = "observed_public"
PA = "published_aggregate"

REGISTRY: list[tuple] = [
    # ---------------- section: reconciliation core ----------------
    ("§", "Reconciliation core"),
    ("recon.as_of_date", "core_build.json", "as_of_date", "str", "Snapshot (as-of) date of the simulated data"),
    ("recon.raw_rows_total", "core_build.json", sum_raw_rows, "int", "Raw document rows landed (all seven files)", S),
    ("recon.raw_rows_crb", "core_build.json", lambda: float(sum(v for k, v in load("core_build.json")["build"]["raw_rows"].items() if k in ("claims_export.csv", "remittance_advice.csv", "bank_statement.csv"))), "int", "Raw rows of the three reconciled documents (claims, remittance, bank)", S),
    ("recon.raw_rows.claims", "core_build.json", "build.raw_rows.claims_export\\.csv", "int", "Raw claim-line rows"),
    ("recon.raw_rows.remits", "core_build.json", "build.raw_rows.remittance_advice\\.csv", "int", "Raw remittance-line rows"),
    ("recon.raw_rows.bank", "core_build.json", "build.raw_rows.bank_statement\\.csv", "int", "Raw bank-statement rows"),
    ("recon.raw_rows.status", "core_build.json", "build.raw_rows.claim_status_history\\.csv", "int", "Raw claim status events"),
    ("recon.raw_rows.invoices", "core_build.json", "build.raw_rows.drug_purchase_invoices\\.csv", "int", "Raw drug invoice lines"),
    ("recon.raw_rows.dispense", "core_build.json", "build.raw_rows.drug_dispense_log\\.csv", "int", "Raw drug dispense rows"),
    ("recon.raw_rows.contracts", "core_build.json", "build.raw_rows.payer_contracts\\.csv", "int", "Raw contract-rate rows"),
    ("recon.claim_lines", "core_build.json", "row_counts.core\\.fact_claim_line", "int", "Claim lines in core", S),
    ("recon.remit_lines", "core_build.json", "row_counts.core\\.fact_remit_line", "int", "Remittance lines in core", S),
    ("recon.bank_txns", "core_build.json", "row_counts.core\\.fact_bank_txn", "int", "Bank transactions in core", S),
    ("recon.claim_events", "core_build.json", "row_counts.core\\.fact_claim_event", "int", "Claim status events in core", S),
    ("recon.staging_rejects", "core_build.json", "row_counts.stg\\.reject", "int", "Raw rows rejected in staging with a reason", S),
    ("recon.exception_rows", "core_build.json", "row_counts.recon\\.exception_queue", "int", "Rows in the exception queue", S),
    ("recon.exception_types", "reconciliation.json", n_exception_types, "int", "Distinct exception types open", S),
    ("recon.claims", "markov.json", "transition_notes.value.n_claims", "int", "Claims (claim headers) in the event feed", S),
    ("recon.control_checks", "reconciliation.json", "tie_out.checks", "int", "Control totals checked each run"),
    ("recon.control_all_passed", "reconciliation.json", "tie_out.all_control_totals_passed", "passed", "Whether every control total passed"),
    ("recon.control_max_diff", "reconciliation.json", "tie_out.max_abs_difference", "usd2", "Largest absolute control-total difference"),
    ("recon.days_closed", "reconciliation.json", "tie_out.days_closed", "int", "Days closed by recon.close_day"),
    ("recon.days_total", "reconciliation.json", "tie_out.days_in_calendar", "int", "Days in the close calendar"),
    ("recon.billed_total", "control_totals.json", lambda: check("CONTROL_IDENTITY_TOTAL"), "usd2", "Billed charges on all core claim lines (identity total)", S),
    ("recon.billed_total_m", "control_totals.json", lambda: check("CONTROL_IDENTITY_TOTAL"), "usdm1", "Billed charges, rounded", S),
    ("recon.remit_paid_total", "control_totals.json", lambda: check("REMIT_PAID_ACCOUNTED"), "usd2", "Remittance paid amounts accounted for", S),
    ("recon.remit_paid_total_m", "control_totals.json", lambda: check("REMIT_PAID_ACCOUNTED"), "usdm1", "Remittance paid, rounded", S),
    ("recon.cash_remit_side", "control_totals.json", lambda: check("CASH_REMIT_SIDE"), "usd2", "Remittance payments dated by the as-of date (cash identity, payer side)", S),
    ("recon.cash_bank_side", "control_totals.json", lambda: check("CASH_BANK_SIDE"), "usd2", "Payer credits on the bank statement (cash identity, bank side)", S),
    ("recon.build_seconds", "core_build.json", "build.timings_s.total", "f0", "Seconds to build the database and close every day", S),
    ("recon.close_seconds", "core_build.json", "build.timings_s.close_days", "f0", "Seconds spent in the close_day loop", S),
    # matching quality
    ("recon.c2r.matched", "reconciliation.json", "claim_to_remittance.overall.matched", "int", "Remittance lines matched to claim lines"),
    ("recon.c2r.precision", "reconciliation.json", "claim_to_remittance.overall.precision", "f4", "Claim-to-remittance precision vs planted truth"),
    ("recon.c2r.recall", "reconciliation.json", "claim_to_remittance.overall.recall_on_postable_lines", "f4", "Claim-to-remittance recall on postable lines"),
    ("recon.c2r.recall_pct", "reconciliation.json", "claim_to_remittance.overall.recall_on_postable_lines", "pct2", "Claim-to-remittance recall, percent"),
    ("recon.c2r.wrong", "reconciliation.json", "claim_to_remittance.wrong_matches.count", "int", "Wrong claim-to-remittance matches"),
    ("recon.c2r.missed", "reconciliation.json", "claim_to_remittance.missed_postable_lines.count", "int", "Postable remittance lines not matched"),
    ("recon.c2r.queue_share", "reconciliation.json", "claim_to_remittance.overall.exception_queue_share_of_postable", "pct2", "Share of postable lines sent to the exception queue"),
    ("recon.r2b.matched", "reconciliation.json", "remittance_to_bank.overall.matched", "int", "Payments matched to bank deposits"),
    ("recon.r2b.expected", "reconciliation.json", "remittance_to_bank.universe.expected_to_match", "int", "Payments that should match a deposit"),
    ("recon.r2b.precision", "reconciliation.json", "remittance_to_bank.overall.precision", "f4", "Remittance-to-bank precision vs planted truth"),
    ("recon.r2b.recall", "reconciliation.json", "remittance_to_bank.overall.recall", "f4", "Remittance-to-bank recall"),
    ("recon.r2b.recall_pct", "reconciliation.json", "remittance_to_bank.overall.recall", "pct1", "Remittance-to-bank recall, percent"),
    ("recon.r2b.wrong", "reconciliation.json", bank_wrong, "int", "Wrong remittance-to-bank matches", S),
    ("recon.dup.precision", "reconciliation.json", "claim_to_remittance.duplicate_detection.precision", "f4", "Duplicate-line detection precision"),
    ("recon.dup.recall", "reconciliation.json", "claim_to_remittance.duplicate_detection.recall", "f4", "Duplicate-line detection recall"),
    ("recon.dup.planted", "reconciliation.json", "claim_to_remittance.duplicate_detection.planted_file_duplicates", "int", "Planted file duplicates"),
    ("recon.dup.flagged", "reconciliation.json", "claim_to_remittance.duplicate_detection.flagged", "int", "Lines flagged as duplicates"),
    ("recon.case.twin.lines", "reconciliation.json", "claim_to_remittance.by_case.transposed_id_with_twin_claim.lines", "int", "Lines with a transposed id and a twin visit"),
    ("recon.case.twin.correct", "reconciliation.json", "claim_to_remittance.by_case.transposed_id_with_twin_claim.matched_correct", "int", "Twin-visit lines matched correctly"),
    ("recon.case.twin.queued", "reconciliation.json", "claim_to_remittance.by_case.transposed_id_with_twin_claim.sent_to_exception_queue", "int", "Twin-visit lines queued"),
    ("recon.case.twin.recall", "reconciliation.json", "claim_to_remittance.by_case.transposed_id_with_twin_claim.recall", "f3", "Recall on twin-visit lines"),
    ("recon.case.repeat.lines", "reconciliation.json", "claim_to_remittance.by_case.repeated_service_without_line_control.lines", "int", "Repeated-service lines without line control number"),
    ("recon.case.repeat.recall", "reconciliation.json", "claim_to_remittance.by_case.repeated_service_without_line_control.recall", "f3", "Recall on repeated services without line control"),
    ("recon.case.equal_untraced.payments", "reconciliation.json", "remittance_to_bank.by_case.equal_amount_provider_payment_untraced.payments", "int", "Equal-amount untraced provider payments"),
    ("recon.case.equal_untraced.correct", "reconciliation.json", "remittance_to_bank.by_case.equal_amount_provider_payment_untraced.matched_correct", "int", "Equal-amount untraced payments matched"),
    ("recon.case.equal_untraced.queued", "reconciliation.json", "remittance_to_bank.by_case.equal_amount_provider_payment_untraced.sent_to_exception_queue", "int", "Equal-amount untraced payments queued"),
    ("recon.case.equal_untraced.recall", "reconciliation.json", "remittance_to_bank.by_case.equal_amount_provider_payment_untraced.recall", "f3", "Recall on equal-amount untraced payments"),
    ("recon.case.split_untraced.payments", "reconciliation.json", "remittance_to_bank.by_case.split_deposit_second_untraced.payments", "int", "Split payments whose second credit has no trace"),
    ("recon.case.split_untraced.recall", "reconciliation.json", "remittance_to_bank.by_case.split_deposit_second_untraced.recall", "f3", "Recall on split payments, second credit untraced"),
    ("recon.case.lockbox.payments", "reconciliation.json", "remittance_to_bank.by_case.lockbox_bundle.payments", "int", "Check payments bundled into lockbox deposits"),
    ("recon.case.truncated.payments", "reconciliation.json", "remittance_to_bank.by_case.truncated_trace.payments", "int", "Payments with a truncated trace in the bank text"),
    ("recon.case.dateshift.payments", "reconciliation.json", "remittance_to_bank.by_case.posting_date_shift.payments", "int", "Payments posted on a shifted bank date"),
    ("recon.case.transposed.lines", "reconciliation.json", "claim_to_remittance.by_case.transposed.lines", "int", "Lines with a transposed claim number (no twin)"),
    ("recon.rule.cr10", "reconciliation.json", "claim_to_remittance.per_rule.CR10_EXACT_CLAIM_ID.matched", "int", "Matches by CR10 exact claim id"),
    ("recon.rule.cr20", "reconciliation.json", "claim_to_remittance.per_rule.CR20_NORMALISED_CLAIM_ID.matched", "int", "Matches by CR20 normalised claim id"),
    ("recon.rule.cr30", "reconciliation.json", "claim_to_remittance.per_rule.CR30_MEMBER_DATE_AMOUNT.matched", "int", "Matches by CR30 member + date + amount"),
    ("recon.rule.rb10", "reconciliation.json", "remittance_to_bank.per_rule.RB10_TRACE_EXACT.payments_matched", "int", "Payments matched by RB10 exact trace"),
    ("recon.rule.rb20", "reconciliation.json", "remittance_to_bank.per_rule.RB20_TRACE_NORMALISED.payments_matched", "int", "Payments matched by RB20 normalised trace"),
    ("recon.rule.rb25", "reconciliation.json", "remittance_to_bank.per_rule.RB25_TRACE_SPLIT.payments_matched", "int", "Payments matched by RB25 split trace"),
    ("recon.rule.rb30", "reconciliation.json", "remittance_to_bank.per_rule.RB30_LOCKBOX_SUBSET.payments_matched", "int", "Payments matched by RB30 lockbox subset"),
    ("recon.rule.rb40", "reconciliation.json", "remittance_to_bank.per_rule.RB40_PAYER_AMOUNT_WINDOW.payments_matched", "int", "Payments matched by RB40 payer + amount + window"),
    ("recon.orphans", "reconciliation.json", "orphan_reversals.orphan_reversals", "int", "Orphan reversals"),
    ("recon.orphans_unexplained", "reconciliation.json", "orphan_reversals.unexplained", "int", "Orphan reversals without an explanation"),
    ("recon.unapplied.n", "core_build.json", lambda: cash_stuck("UNAPPLIED_CASH", "n_items"), "int", "Unapplied cash items", S),
    ("recon.unapplied.amount", "core_build.json", lambda: cash_stuck("UNAPPLIED_CASH", "amount"), "usd2", "Unapplied cash amount", S),
    ("recon.unapplied.true", "reconciliation.json", "remittance_to_bank.unapplied_cash.planted_deposits_without_remittance", "int", "Unapplied credits that are true deposits without a remittance (planted, all queued)"),
    ("recon.unapplied.ambiguous", "reconciliation.json", "remittance_to_bank.unapplied_cash.queued_with_reason.AMBIGUOUS", "int", "Unapplied credits queued as AMBIGUOUS (two equal candidates)"),
    ("recon.unapplied.split", "reconciliation.json", "remittance_to_bank.unapplied_cash.queued_with_reason.POSSIBLE SPLIT OR PARTIAL DEPOSIT", "int", "Unapplied credits queued as POSSIBLE SPLIT"),
    ("recon.unapplied.manual", "reconciliation.json", lambda: cash_stuck("UNAPPLIED_CASH", "n_items") - load("reconciliation.json")["remittance_to_bank"]["unapplied_cash"]["planted_deposits_without_remittance"], "int", "Unapplied credits that are queued for manual matching rather than true deposits without remittance", S),
    ("recon.remit_no_deposit.true", "reconciliation.json", "remittance_to_bank.by_case.deposit_missing.correctly_left_unmatched", "int", "Payments whose deposit truly never arrived (planted)"),
    ("recon.remit_no_deposit.manual", "reconciliation.json", lambda: cash_stuck("REMIT_NO_DEPOSIT", "n_items") - load("reconciliation.json")["remittance_to_bank"]["by_case"]["deposit_missing"]["correctly_left_unmatched"], "int", "Payments without a matched deposit because the rules queued the candidate deposit (ambiguous or split)", S),
    ("recon.remit_no_deposit.n", "core_build.json", lambda: cash_stuck("REMIT_NO_DEPOSIT", "n_items"), "int", "Payments with no deposit", S),
    ("recon.remit_no_deposit.amount", "core_build.json", lambda: cash_stuck("REMIT_NO_DEPOSIT", "amount"), "usd2", "Amount remitted with no deposit", S),
    ("recon.denied_in_play.n", "core_build.json", lambda: cash_stuck("DENIED_IN_PLAY", "n_items"), "int", "Denied lines still workable", S),
    ("recon.denied_in_play.amount", "core_build.json", lambda: cash_stuck("DENIED_IN_PLAY", "amount"), "usd2", "Denied amount still workable", S),
    ("recon.denied_written_off.amount", "core_build.json", lambda: cash_stuck("DENIED_WRITTEN_OFF", "amount"), "usd2", "Denied amount written off", S),
    ("recon.underpaid.n", "core_build.json", lambda: cash_stuck("UNDERPAID_VS_CONTRACT", "n_items"), "int", "Lines underpaid versus contract (tolerance $1)", S),
    ("recon.underpaid.amount", "core_build.json", lambda: cash_stuck("UNDERPAID_VS_CONTRACT", "amount"), "usd2", "Underpaid amount versus contract (tolerance $1)", S),
    ("recon.plb.amount", "core_build.json", lambda: cash_stuck("PROVIDER_LEVEL_SUSPENSE", "amount"), "usd2", "Provider-level payments held in suspense", S),
    ("recon.not_adjudicated.amount", "core_build.json", lambda: cash_stuck("NOT_YET_ADJUDICATED", "amount"), "usd2", "Open balance not yet adjudicated", S),

    # ---------------- public calibration ----------------
    ("§", "Public calibration inputs"),
    ("public.denial_rate_min", "core_build.json", lambda: min(o["applied_value"] for o in load("core_build.json")["calibration"]["overrides"] if o["parameter"] == "denial.first_pass_denial_rate"), "pct1", "Lowest simulated payer first-pass denial rate, set at a percentile of the CMS issuer distribution (calibrated assumption)", FA),
    ("public.denial_rate_max", "core_build.json", lambda: max(o["applied_value"] for o in load("core_build.json")["calibration"]["overrides"] if o["parameter"] == "denial.first_pass_denial_rate"), "pct1", "Highest simulated payer first-pass denial rate, set at a percentile of the CMS issuer distribution (calibrated assumption)", FA),
    ("public.cms_p10", "config/public_calibration.yaml", "denial_rate_in_network.p10", "pct1", "CMS TC-PUF issuer in-network denial rate, 10th percentile (computed by this project from CMS data)", OP),
    ("public.cms_p90", "config/public_calibration.yaml", "denial_rate_in_network.p90", "pct1", "CMS TC-PUF issuer in-network denial rate, 90th percentile (computed by this project from CMS data)", OP),
    ("public.cms_n_issuers", "config/public_calibration.yaml", "denial_rate_in_network.n_issuers", "int", "Issuers in the CMS distribution", OP),
    ("public.cms_year", "config/public_calibration.yaml", "denial_rate_in_network.plan_year", "str", "Data year of the CMS distribution", OP),
    ("public.sofr_as_of", "config/public_calibration.yaml", "interest_rates.sofr.as_of", "str", "Date of the SOFR observation used", OP),
    ("public.sofr", "econ.json", lambda: next(o["applied_value"] for o in load("core_build.json")["calibration"]["overrides"] if "discount_rate" in o["parameter"]), "pct2", "SOFR used as the discount rate (FRED, latest observation at retrieval)", OP),
    ("public.retrieved", "core_build.json", "calibration.public_retrieved", "str", "Retrieval date of the public files", OP),
    ("public.n_drugs", "drug.json", "inputs.reference_prices.n_drugs", "int", "Drugs with a CMS Part B payment limit as reference price", PA),
    ("public.asp_quarter", "drug.json", "inputs.reference_prices.quarter", "str", "Quarter of the CMS payment limit file", PA),

    # ---------------- Markov chain ----------------
    ("§", "Claim life cycle: absorbing Markov chain"),
    ("markov.n_transitions", "markov.json", "transition_notes.value.n_transitions", "int", "Observed state transitions used to estimate the matrix"),
    ("markov.maturity_days", "markov.json", "inputs.maturity_days", "int", "Exits from states entered fewer days than this before the cutoff are dropped (assumption)", FA),
    ("markov.n_states", "markov.json", lambda: len(load("markov.json")["states"]), "int", "States in the chain (5 transient + 2 absorbing)", M),
    ("markov.p_paid", "markov.json", "per_payer.ALL.p_paid_from_submitted.value", "pct1", "Probability a submitted claim is eventually paid, all payers"),
    ("markov.p_paid_ci", "markov.json", "per_payer.ALL.p_paid_from_submitted_95.value", "ci_pct1", "95% claim-level bootstrap interval"),
    ("markov.p_paid_min", "markov.json", lambda: markov_paid_range("min"), "pct1", "Lowest payer probability of eventual payment", M),
    ("markov.p_paid_max", "markov.json", lambda: markov_paid_range("max"), "pct1", "Highest payer probability of eventual payment", M),
    ("markov.p_paid_from_denied", "markov.json", "per_payer.ALL.absorption_B.value.DENIED.PAID", "pct1", "Probability of eventual payment once a claim is denied"),
    ("markov.steps_from_submitted", "markov.json", "per_payer.ALL.expected_transitions_to_absorption.value.SUBMITTED", "f2", "Expected transitions from SUBMITTED to PAID or WRITTEN_OFF"),
    ("markov.P.denied_appealed", "markov.json", "per_payer.ALL.matrix.value.DENIED.APPEALED", "f4", "Transition probability DENIED -> APPEALED"),
    ("markov.P.denied_writeoff", "markov.json", "per_payer.ALL.matrix.value.DENIED.WRITTEN_OFF", "f4", "Transition probability DENIED -> WRITTEN_OFF"),
    ("markov.P.denied_review", "markov.json", "per_payer.ALL.matrix.value.DENIED.IN_REVIEW", "f4", "Transition probability DENIED -> IN_REVIEW (resubmitted)"),
    ("markov.P.appealed_paid", "markov.json", "per_payer.ALL.matrix.value.APPEALED.PAID", "f4", "Transition probability APPEALED -> PAID"),
    ("markov.P.review_denied", "markov.json", "per_payer.ALL.matrix.value.IN_REVIEW.DENIED", "f4", "Transition probability IN_REVIEW -> DENIED"),
    ("markov.P.review_paid", "markov.json", "per_payer.ALL.matrix.value.IN_REVIEW.PAID", "f4", "Transition probability IN_REVIEW -> PAID"),
    ("markov.brier_constant", "markov.json", "holdout_calibration.value.brier_score_constant_rate", "f4", "Hold-out Brier score, constant paid rate"),
    ("markov.brier_memoryless", "markov.json", "holdout_calibration.value.memoryless.brier_score", "f4", "Hold-out Brier score, plain Markov chain"),
    ("markov.brier_duration", "markov.json", "holdout_calibration.value.duration_adjusted.brier_score", "f4", "Hold-out Brier score, chain with time-in-state correction"),
    ("markov.denied_n", "markov.json", "holdout_calibration.value.memoryless.by_state_at_cutoff.DENIED.n", "int", "Hold-out claims sitting in DENIED at the cutoff"),
    ("markov.denied_pred_memoryless", "markov.json", "holdout_calibration.value.memoryless.by_state_at_cutoff.DENIED.predicted_p_paid", "pct1", "Plain chain: predicted P(paid) for claims in DENIED at cutoff"),
    ("markov.denied_observed", "markov.json", "holdout_calibration.value.memoryless.by_state_at_cutoff.DENIED.observed_p_paid", "pct1", "Observed share paid for those claims"),
    ("markov.denied_pred_duration", "markov.json", "holdout_calibration.value.duration_adjusted.by_state_at_cutoff.DENIED.predicted_p_paid", "pct1", "Time-in-state corrected prediction for those claims"),
    ("markov.max_abs_error", "markov_validation.json", "maturity_180_days.value.max_abs_error_all_payers", "f3", "Largest absolute error of an estimated transition probability vs planted (all payers)"),
    ("markov.planted_inside", "markov_validation.json", "maturity_180_days.value.n_planted_in_bootstrap_95", "int", "Payers (of 6) whose planted P(paid) is inside the bootstrap 95% interval"),
    ("markov.chi2_after_denied", "markov.json", "markov_assumption_diagnostics.value.after_denied_by_reason.chi2", "f0", "Chi-square: next step after DENIED depends on denial reason"),
    ("markov.review_denied_first", "markov.json", "markov_assumption_diagnostics.value.in_review_outcome_by_visit.p_denied.first", "pct1", "P(denied) on the first review"),
    ("markov.review_denied_later", "markov.json", "markov_assumption_diagnostics.value.in_review_outcome_by_visit.p_denied.later", "pct1", "P(denied) on a later review"),

    # ---------------- cash forecast ----------------
    ("§", "Q3 Treasurer: 13-week cash forecast"),
    ("cash.cov80", "cashflow.json", "backtest.value.summary.hybrid.weekly_80.coverage", "pct1", "Backtest: share of forecast weeks inside the 80% interval"),
    ("cash.cov95", "cashflow.json", "backtest.value.summary.hybrid.weekly_95.coverage", "pct1", "Backtest: share of forecast weeks inside the 95% interval"),
    ("cash.cov80_ci", "cashflow.json", "backtest.value.summary.hybrid.weekly_80.wilson_95", "ci_pct1", "Wilson 95% interval of the 80% coverage (too narrow: weeks overlap)"),
    ("cash.n_forecasts", "cashflow.json", "backtest.value.summary.hybrid.n_origins", "int", "Backtest forecast origins (weekly)"),
    ("cash.n_weeks", "cashflow.json", "backtest.value.summary.hybrid.weekly_80.n", "int", "Backtest forecast-weeks scored"),
    ("cash.cov80_total", "cashflow.json", "backtest.value.summary.hybrid.total_13w_80_all_origins.coverage", "pct1", "Backtest: 13-week totals inside the 80% interval"),
    ("cash.week1_cov80", "cashflow.json", "backtest.value.summary.hybrid.week1_80.coverage", "pct1", "Backtest: first forecast week inside the 80% interval"),
    ("cash.mape_13w", "cashflow.json", "backtest.value.summary.hybrid.total_13w_mape", "pct1", "Backtest: mean absolute % error of the 13-week total"),
    ("cash.mape_weekly", "cashflow.json", "backtest.value.summary.hybrid.weekly_mape", "pct1", "Backtest: mean absolute % error per week"),
    ("cash.question_date", "cashflow.json", "shortfall.value.forecast_date", "str", "Date at which the Treasurer's question is asked (last backtest date, hospital still billing)"),
    ("cash.car95", "cashflow.json", "shortfall.value.cash_at_risk_95", "usd0", "Cash-at-risk (95%): mean minus 5th percentile of 13-week receipts"),
    ("cash.receipts_mean", "cashflow.json", "shortfall.value.receipts_13w_mean", "usd0", "Mean simulated 13-week receipts"),
    ("cash.receipts_p05", "cashflow.json", "shortfall.value.receipts_13w_p05", "usd0", "5th percentile of 13-week receipts"),
    ("cash.receipts_actual", "cashflow.json", "shortfall.value.actual_13w_receipts", "usd0", "Receipts that actually arrived in those 13 weeks (simulated)"),
    ("cash.actual_percentile", "cashflow.json", "shortfall.value.actual_receipts_percentile_in_forecast", "pct0", "Percentile of the actual receipts inside the forecast distribution"),
    ("cash.weekly_need", "cashflow.json", "shortfall.value.weekly_cash_need", "usd0", "Assumed weekly cash need (factor x trailing median receipts)"),
    ("cash.need_factor", "cashflow.json", "assumptions.cash_need_factor.value", "f2", "Cash-need factor (assumption)"),
    ("cash.min_balance", "cashflow.json", "assumptions.minimum_operating_balance_usd.value", "usd0", "Minimum operating balance (assumption)"),
    ("cash.p_shortfall", "cashflow.json", "shortfall.value.p_shortfall", "pct1", "Probability of a shortfall below the minimum balance in 13 weeks"),
    ("cash.cvar95", "cashflow.json", "shortfall.value.shortfall_cvar95", "usd0", "CVaR (95%) of the shortfall"),
    ("cash.run_off", "cashflow.json", "forecast_is_run_off.value", "yesno", "Is the as-of forecast a run-off of the open book?"),
    ("cash.n_sims", "cashflow.json", "inputs.params.n_sims", "int", "Monte Carlo paths", M),
    ("cash.lognormal_best", "cashflow.json", lognormal_best, "str", "Payers whose payment lag is best fit by a lognormal (AIC)", M),
    ("cash.km_median_p01", "cashflow.json", "lag_model.value.P01.km_median_days", "g", "Kaplan-Meier median payment lag, P01 (days)"),
    ("cash.km_median_p06", "cashflow.json", "lag_model.value.P06.km_median_days", "g", "Kaplan-Meier median payment lag, P06 (days)"),
    ("cash.lag_quartile_error", "cashflow_validation.json", "first_pass_lag.value.2026-03-31.max_abs_error_quartiles_days", "g", "Largest error of estimated first-pass lag quartiles vs generator (days)", M),

    # ---------------- borrowing base ----------------
    ("§", "Q5 Bank credit analyst: borrowing base"),
    ("credit.certificate_date", "credit.json", "certificate_steady_state.value.certificate_date", "str", "Date of the steady-state borrowing-base certificate"),
    ("credit.gross_ncv", "credit.json", "certificate_steady_state.value.totals.gross_ncv", "usd0", "Gross net collectible value of open receivables"),
    ("credit.eligible", "credit.json", "certificate_steady_state.value.totals.eligible", "usd0", "Eligible receivables after exclusions and concentration cap"),
    ("credit.availability", "credit.json", "certificate_steady_state.value.totals.availability", "usd0", "Borrowing availability (advance rate x eligible - reserves)"),
    ("credit.expected_loss", "credit.json", "certificate_steady_state.value.totals.expected_loss", "usd0", "Expected loss PD x LGD x EAD on the eligible pool"),
    ("credit.el_covered", "credit.json", "certificate_steady_state.value.expected_loss_covered_by_margin", "yesno", "Is expected loss covered by the lender's margin (1 - advance rate) x eligible?"),
    ("credit.advance_rate", "credit.json", "assumptions.advance_rate.value", "pct0", "Advance rate (assumption)"),
    ("credit.concentration_limit", "credit.json", "assumptions.concentration_limit.value", "pct0", "Single-payer concentration limit (assumption)"),
    ("credit.max_age_days", "credit.json", "assumptions.eligibility_max_age_days.value", "int", "Age limit for eligibility, days since submission (assumption)"),
    ("credit.spread", "credit.json", "assumptions.facility_spread.value", "pct2", "Facility spread over SOFR (assumption)"),
    ("credit.shock_small", "credit.json", "inputs.params.rate_shocks_bp[0]", "int", "Smaller interest-rate shock, basis points", M),
    ("credit.shock_large", "credit.json", "inputs.params.rate_shocks_bp[1]", "int", "Larger interest-rate shock, basis points", M),
    ("credit.slow_pct", "credit.json", lambda: load("credit.json")["inputs"]["params"]["slow_factor"] - 1, "pct0", "Slower-payment stress: lags stretched by this share", M),
    ("credit.tie_diff", "credit.json", "tie_out_point_in_time_vs_recon.value.abs_difference_total", "usd2", "Point-in-time open balance vs reconciled balance, absolute difference"),
    ("credit.open_total", "credit.json", "tie_out_point_in_time_vs_recon.value.point_in_time_open_total", "usd2", "Open balance at the as-of date (ties to recon)"),
    ("credit.inel_denied", "credit.json", "certificate_steady_state.value.totals.inel_denied", "usd0", "Ineligible: denied"),
    ("credit.inel_disputed", "credit.json", "certificate_steady_state.value.totals.inel_disputed", "usd0", "Ineligible: disputed (appealed or pended)"),
    ("credit.inel_aged", "credit.json", "certificate_steady_state.value.totals.inel_aged", "usd0", "Ineligible: past the age limit"),
    ("credit.credit_balance_reserve", "credit.json", "certificate_steady_state.value.totals.credit_balance_reserve", "usd0", "Reserve for credit balances owed back to payers"),
    ("credit.lender_margin", "credit.json", "certificate_steady_state.value.lender_margin_eligible_x_(1-advance)", "usd0", "Lender margin (1 - advance rate) x eligible"),
    ("credit.asof_availability", "credit.json", "certificate_as_of.value.totals.availability", "usd0", "Availability at the as-of date (run-off book)"),
    ("credit.sens90_availability", "credit.json", "sensitivity_90_day_limit_steady_state.value.availability", "usd0", "Availability with a 90-day age limit"),
    ("credit.stress_slow_200bp_availability", "credit.json", "stress_steady_state.value[scenario=+200bp_and_slower].availability", "usd0", "Availability with +200 bp and 25% slower payment"),
    ("credit.stress_200bp_interest", "credit.json", "stress_steady_state.value[scenario=+200bp].interest_cost_annual", "usd0", "Annual interest on the drawn facility at +200 bp"),
    ("credit.base_interest", "credit.json", "stress_steady_state.value[scenario=base].interest_cost_annual", "usd0", "Annual interest on the drawn facility, base case"),
    ("credit.base_rate", "credit.json", "stress_steady_state.value[scenario=base].rate_annual", "pct2", "Facility rate, base case (SOFR + spread)"),
    ("credit.slow_200bp_carry", "credit.json", "stress_steady_state.value[scenario=+200bp_and_slower].carry_cost_of_delay", "usd0", "Financing cost of waiting for open receivables, +200 bp and slower"),
    ("credit.base_carry", "credit.json", "stress_steady_state.value[scenario=base].carry_cost_of_delay", "usd0", "Financing cost of waiting for open receivables, base"),
    ("credit.el_bt_predicted", "credit.json", "expected_loss_backtest.value.predicted_expected_loss", "usd0", "Expected loss predicted on the steady-state eligible pool"),
    ("credit.el_bt_realised", "credit.json", "expected_loss_backtest.value.realised_loss_written_off", "usd0", "Loss actually written off on that pool by the as-of date"),
    ("credit.collected_share", "credit.json", "expected_loss_backtest.value.collected_share_of_ncv", "pct1", "Share of eligible NCV collected after the certificate"),
    ("credit.pd_ncv", "credit_validation.json", "pd_vs_ultimate_outcome.value.predicted_pd_ncv_weighted", "pct1", "Predicted PD, NCV-weighted"),
    ("credit.wo_ncv", "credit_validation.json", "pd_vs_ultimate_outcome.value.ultimate_write_off_share_ncv", "pct1", "Generator's ultimate write-off share, NCV-weighted"),
    ("credit.pd_count", "credit_validation.json", "pd_vs_ultimate_outcome.value.predicted_pd_count", "pct1", "Predicted PD, count-weighted"),
    ("credit.wo_count", "credit_validation.json", "pd_vs_ultimate_outcome.value.ultimate_write_off_rate_count", "pct1", "Generator's ultimate write-off rate, count-weighted"),

    # ---------------- denial prioritisation ----------------
    ("§", "Q2 Revenue-cycle lead: denial work queue"),
    ("q2.train_appeals", "prioritize.json", "data.train_appeals.value", "int", "Appeals in the training set (decided before the cutoff)"),
    ("q2.test_appeals", "prioritize.json", "data.test_appeals.value", "int", "Appeals in the held-out test set"),
    ("q2.cutoff", "prioritize.json", "validation_cutoff", "str", "Validation cutoff date", M),
    ("q2.auc_model", "prioritize.json", "validation.held_out_metrics.logistic_model.auc", "f3", "Held-out AUC, logistic model"),
    ("q2.auc_reason", "prioritize.json", "validation.held_out_metrics.baseline_reason_rate.auc", "f3", "Held-out AUC, reason-category lookup table"),
    ("q2.auc_planted", "prioritize_evaluation.json", "planted_probability_metrics_on_test.auc", "f3", "Held-out AUC of the generator's own probabilities (ceiling)"),
    ("q2.brier_model", "prioritize.json", "validation.held_out_metrics.logistic_model.brier", "f4", "Held-out Brier, logistic model"),
    ("q2.brier_reason", "prioritize.json", "validation.held_out_metrics.baseline_reason_rate.brier", "f4", "Held-out Brier, reason-category lookup table"),
    ("q2.brier_overall", "prioritize.json", "validation.held_out_metrics.baseline_overall_rate.brier", "f4", "Held-out Brier, overall rate"),
    ("q2.calib_slope", "prioritize.json", "validation.held_out_metrics.logistic_model.calibration_slope", "f2", "Calibration slope, logistic model"),
    ("q2.calib_slope_ci", "prioritize.json", "validation.held_out_metrics.logistic_model.calibration_slope_ci95", "ci_f2", "95% interval of the calibration slope"),
    ("q2.coef_coverage", "prioritize_evaluation.json", "parameter_recovery.coefficients_fit_at_cutoff.ci95_coverage", "str", "Planted coefficients inside the 95% CI"),
    ("q2.coef_corr", "prioritize_evaluation.json", "parameter_recovery.coefficients_fit_at_cutoff.corr_estimate_planted", "f2", "Correlation of estimates with planted coefficients"),
    ("q2.recoverable_ratio", "prioritize.json", "validation.recoverable_amount_check.realised_over_predicted", "f3", "Realised payment / predicted recoverable on overturned appeals"),
    ("q2.today_candidates", "prioritize.json", "todays_queue.candidates.value", "int", "Denied claims awaiting action on the as-of date"),
    ("q2.today_selected", "prioritize.json", "todays_queue.selected.value", "int", "Claims chosen by the knapsack today"),
    ("q2.today_minutes", "prioritize.json", "todays_queue.selected_minutes.value", "int", "Minutes used of the daily budget"),
    ("q2.budget_minutes", "prioritize.json", "todays_queue.budget_minutes.value", "int", "Daily staff budget in minutes (assumption)"),
    ("q2.today_value", "prioritize.json", "todays_queue.policies_same_budget.MODEL_KNAPSACK.expected_net", "usd0", "Expected net value of today's knapsack list", M),
    ("q2.today_oldest", "prioritize.json", "todays_queue.policies_same_budget.OLDEST_FIRST.expected_net", "usd0", "Same budget, oldest first", M),
    ("q2.today_largest", "prioritize.json", "todays_queue.policies_same_budget.LARGEST_FIRST.expected_net", "usd0", "Same budget, largest first", M),
    ("q2.bt1h_knap_planted", "prioritize_evaluation.json", "backtest_value_under_planted_probabilities[hours_per_day=1.0].model_knapsack", "usd0", "Backtest at 1 h/day, knapsack choices valued with planted probabilities"),
    ("q2.bt1h_largest_planted", "prioritize_evaluation.json", "backtest_value_under_planted_probabilities[hours_per_day=1.0].largest_first", "usd0", "Same, largest first"),
    ("q2.bt1h_oldest_planted", "prioritize_evaluation.json", "backtest_value_under_planted_probabilities[hours_per_day=1.0].oldest_first", "usd0", "Same, oldest first"),
    ("q2.bt1h_oracle", "prioritize_evaluation.json", "oracle_knapsack[hours_per_day=1.0].oracle_knapsack_true_value", "usd0", "Knapsack that knows the planted probabilities, 1 h/day"),
    ("q2.bt1h_knap_realised", "prioritize.json", "backtest.realised_net_appealed_with_outcome[hours_per_day=1.0].model_knapsack", "usd0", "Realised net dollars on appealed claims, knapsack, 1 h/day"),
    ("q2.bt1h_largest_realised", "prioritize.json", "backtest.realised_net_appealed_with_outcome[hours_per_day=1.0].largest_first", "usd0", "Same, largest first"),
    ("q2.bt1h_oldest_realised", "prioritize.json", "backtest.realised_net_appealed_with_outcome[hours_per_day=1.0].oldest_first", "usd0", "Same, oldest first"),
    ("q2.denials_per_day", "prioritize.json", "data.held_out_denials_per_business_day.value", "f1", "Held-out denials per business day"),
    ("q2.appeal_cost", "prioritize.json", "assumptions.handling_cost_per_appeal_usd.value", "usd0", "Cost of one appeal (assumption)"),
    ("q2.appeal_window", "prioritize.json", "assumptions.appeal_window_days.value", "int", "Appeal window in days (assumption)"),

    # ---------------- reserve ----------------
    ("§", "Q4 Payer actuary: unpaid claims reserve"),
    ("q4.triangle_paid", "reserve.json", "triangle.total_paid_to_date.value", "usd2", "Paid to date in the development triangle"),
    ("q4.triangle_tie", "reserve.json", "triangle.tie_to_claim_line_balance_paid.value", "usd2", "Triangle total minus reconciled paid (should be 0)"),
    ("q4.cl_unpaid", "reserve.json", "chain_ladder.total_unpaid.value", "usd0", "Chain-ladder unpaid estimate"),
    ("q4.cl_unpaid_k", "reserve.json", "chain_ladder.total_unpaid.value", "usdk", "Chain-ladder unpaid, rounded"),
    ("q4.mack_se", "reserve.json", "chain_ladder.mack_se_total.value", "usd0", "Mack standard error of the total reserve"),
    ("q4.mack_cv", "reserve.json", "chain_ladder.mack_cv.value", "pct0", "Mack coefficient of variation"),
    ("q4.boot_p05", "reserve.json", "chain_ladder.bootstrap.p05.value", "usd0", "ODP bootstrap 5th percentile"),
    ("q4.boot_p95", "reserve.json", "chain_ladder.bootstrap.p95.value", "usd0", "ODP bootstrap 95th percentile"),
    ("q4.bf_unpaid", "reserve.json", "chain_ladder.bf_cape_cod_unpaid.value", "usd0", "Bornhuetter-Ferguson (Cape Cod) unpaid"),
    ("q4.elr", "reserve.json", "chain_ladder.bf_cape_cod_elr.value", "f3", "Cape Cod expected ratio (paid / contract allowed)"),
    ("q4.f0", "reserve.json", "chain_ladder.age_to_age[0].f", "f2", "Age-to-age factor, lag 0 to 1"),
    ("q4.cdf0", "reserve.json", "chain_ladder.cdf_by_lag.0", "f2", "Cumulative development factor at lag 0"),
    ("q4.true_unpaid", "reserve_evaluation.json", "true_unpaid_total.value", "usd0", "Simulator's true unpaid amount (evaluation only)"),
    ("q4.cl_error", "reserve_evaluation.json", lambda: load("reserve_evaluation.json")["estimates"]["chain_ladder"]["error_pct"] / 100, "pct0", "Chain-ladder error versus true unpaid", M),
    ("q4.bf_error", "reserve_evaluation.json", lambda: load("reserve_evaluation.json")["estimates"]["bornhuetter_ferguson_cape_cod"]["error_pct"] / 100, "pct0", "BF error versus true unpaid", M),
    ("q4.true_inside", "reserve_evaluation.json", "estimates.true_inside_bootstrap_90pct", "yesno", "True unpaid inside the bootstrap 90% interval"),
    ("q4.error_se", "reserve_evaluation.json", "estimates.error_in_mack_se_units", "f2", "Chain-ladder error in Mack standard errors"),
    ("q4.top10_share", "reserve_evaluation.json", "true_unpaid_concentration.top10_claims_share", "pct0", "Share of true unpaid held by the 10 largest claims"),
    ("q4.months_inside", "reserve_evaluation.json", "accident_months_inside_90pct", "str", "Recent accident months inside their own 90% interval"),
    ("q4.retro_inside", "reserve.json", "retrospective.observed_inside_90pct_interval", "str", "Retrospective dates where later payments fell inside the interval"),
    ("q4.poisson_dispersion", "reserve.json", "frequency_severity.frequency.pearson_dispersion", "f2", "Pearson dispersion of claim counts (1 = Poisson)"),

    # ---------------- drugs ----------------
    ("§", "Q6 Pharmacy manager: drug margin with FIFO"),
    ("q6.mature_days", "drug.json", "cutoff.mature_days.value", "int", "Days before the as-of date after which drug claims count as settled (assumption)"),
    ("q6.n_lots", "drug.json", "validation.n_lots.value", "int", "Drug lots (invoice lines)"),
    ("q6.lot_identity_units", "drug.json", "validation.lot_identity_units_all_lots.value", "yesno", "Purchased = administered + wasted + on hand on every lot, in units"),
    ("q6.lot_identity_value", "drug.json", "validation.lot_identity_value_all_lots.value", "yesno", "Same identity on every lot to the cent"),
    ("q6.units_purchased", "drug.json", "validation.units.purchased.value", "int", "Billing units purchased"),
    ("q6.units_administered", "drug.json", "validation.units.administered.value", "int", "Billing units administered"),
    ("q6.units_expired", "drug.json", "validation.units.expired_waste.value", "int", "Billing units expired"),
    ("q6.units_on_hand", "drug.json", "validation.units.on_hand.value", "int", "Billing units on hand"),
    ("q6.usd_purchased", "drug.json", "validation.dollars.purchased.value", "usd2", "Invoice value purchased"),
    ("q6.usd_administered", "drug.json", "validation.dollars.administered_fifo_cost.value", "usd2", "FIFO cost of administered units"),
    ("q6.usd_expired", "drug.json", "validation.dollars.expired_waste.value", "usd2", "FIFO cost of expired units"),
    ("q6.usd_on_hand", "drug.json", "validation.dollars.on_hand.value", "usd2", "FIFO cost of units on hand"),
    ("q6.lots_exact_share", "drug_validation.json", "share_lots_exact_all_quantities.value", "pct2", "Lots agreeing with the generator on every quantity"),
    ("q6.n_losing", "drug.json", "totals.MATURE.n_drugs_losing_money.value", "int", "Drugs losing money after reimbursement (mature cohort)"),
    ("q6.n_drugs", "drug.json", "totals.MATURE.n_drugs", "int", "Drugs analysed", M),
    ("q6.margin", "drug.json", "totals.MATURE.margin_collected.value", "usd2", "Margin, mature cohort (payer paid - FIFO cost - expired)"),
    ("q6.margin_m", "drug.json", "totals.MATURE.margin_collected.value", "usdm1", "Margin, rounded"),
    ("q6.price_gap", "drug.json", "totals.MATURE.price_gap_contract_minus_cost.value", "usd2", "Contract value minus FIFO cost"),
    ("q6.price_gap_m", "drug.json", "totals.MATURE.price_gap_contract_minus_cost.value", "usdm1", "Contract value minus FIFO cost, rounded"),
    ("q6.denied_leak", "drug.json", "totals.MATURE.denied_leak.value", "usd2", "Contract value lost to denied drug claims"),
    ("q6.denied_leak_m", "drug.json", "totals.MATURE.denied_leak.value", "usdm1", "Denied drug value, rounded"),
    ("q6.patient_resp", "drug.json", "totals.MATURE.patient_resp.value", "usd2", "Patient share on drug lines (not counted as collected)"),
    ("q6.n_losing_if_patient_pays", "drug.json", drugs_losing_if_patient_pays, "int", "Drugs still losing money if every patient share were collected", M),
    ("q6.denied_share", "drug.json", "headline.denied_dollar_share_observed.value", "pct1", "Share of drug contract dollars on finally denied lines"),
    ("q6.breakeven", "drug.json", "headline.breakeven_denial_share_total.value", "pct1", "Final-denial share below which the drug book turns profitable"),
    ("q6.sens_d25", "drug.json", "sensitivity_mature.by_final_denial_rate[final_denial_rate=0.25].total_margin", "usd2", "Drug margin at a 25% final-denial share"),
    ("q6.sens_d15", "drug.json", "sensitivity_mature.by_final_denial_rate[final_denial_rate=0.15].total_margin", "usd2", "Drug margin at a 15% final-denial share"),
    ("q6.sens_c125", "drug.json", "sensitivity_mature.by_contract_pct_of_reference[contract_pct_of_reference=1.25].total_margin", "usd2", "Drug margin with contracts at 125% of the CMS limit"),
    ("q6.sens_c135", "drug.json", "sensitivity_mature.by_contract_pct_of_reference[contract_pct_of_reference=1.35].total_margin", "usd2", "Drug margin with contracts at 135% of the CMS limit"),
    ("q6.top_loser", "drug.json", "drugs_mature.rows[0].hcpcs", "str", "Drug with the largest loss (mature cohort)", M),
    ("q6.top_loser_name", "drug.json", "drugs_mature.rows[0].short_description", "str", "Its CMS short description", PA),
    ("q6.top_loser_margin", "drug.json", "drugs_mature.rows[0].margin_collected", "usd2", "Its margin", M),
    ("q6.top_loser_denied_share", "drug.json", "drugs_mature.rows[0].denied_line_share", "pct0", "Share of its claim lines denied", S),
    ("q6.p04_below_cost", "drug.json", "contract_below_cost_by_payer.rows[payer_id=P04].n_drugs_rate_below_cost", "int", "Drugs where P04's contract rate is below FIFO cost"),
    ("q6.fifo_cost", "drug.json", "validation.fifo_vs_recorded_lot.fifo_cost.value", "usd2", "FIFO cost of administrations with a recorded lot"),
    ("q6.si_cost", "drug.json", "validation.fifo_vs_recorded_lot.specific_identification_cost.value", "usd2", "Specific-identification cost of the same administrations"),

    # ---------------- economics ----------------
    ("§", "Q7 Consulting economist: underpayment, float, incentives"),
    ("q7.shortfall", "econ.json", "headline.total_pricing_shortfall_usd.value", "usd2", "Total shortfall at first pricing versus contract"),
    ("q7.shortfall_k", "econ.json", "headline.total_pricing_shortfall_usd.value", "usdk", "Total shortfall, rounded"),
    ("q7.top_payer", "econ.json", "headline.payer_highest_shortfall_rate.value", "str", "Payer with the highest shortfall rate"),
    ("q7.top_rate", "econ.json", "headline.highest_shortfall_rate.value", "pct2", "Its shortfall rate (shortfall / contract)"),
    ("q7.top_rate_ci", "econ.json", "headline.highest_shortfall_rate_ci.value", "ci_pct2", "95% bootstrap interval (whole claims resampled)"),
    ("q7.p03_share", "econ.json", "underpayment_by_payer.rows[payer_id=P03].underpaid_claim_share", "pct1", "Share of P03's priced claims underpaid"),
    ("q7.p03_shortfall", "econ.json", "underpayment_by_payer.rows[payer_id=P03].shortfall_usd", "usd2", "P03 shortfall dollars"),
    ("q7.ranking", "econ.json", "headline.payers_ranked_by_shortfall_rate.value", "list", "Payers ranked by shortfall rate"),
    ("q7.flag_precision", "econ_validation.json", "line_classification_vs_generator.precision.value", "f4", "Underpayment line flags: precision vs generator"),
    ("q7.flag_recall", "econ_validation.json", "line_classification_vs_generator.recall.value", "f4", "Underpayment line flags: recall vs generator"),
    ("q7.planted_share_inside", "econ_validation.json", "planted_share_inside_ci.value", "int", "Payers (of 6) whose planted underpayment probability is inside the 95% CI"),
    ("q7.planted_mean_inside", "econ_validation.json", "planted_mean_inside_ci.value", "int", "Payers (of 6) whose planted mean shortfall is inside the 95% CI"),
    ("q7.float_total", "econ.json", "headline.total_float_total_usd.value", "usd2", "Value of payment float at SOFR"),
    ("q7.float_late", "econ.json", "headline.total_float_late_usd.value", "usd2", "Float beyond the 30-day window"),
    ("q7.threshold_min", "econ.json", lambda: econ_rows("threshold_usd", "min"), "usd2", "Lowest appeal break-even threshold c / P(win)", M),
    ("q7.threshold_max", "econ.json", lambda: econ_rows("threshold_usd", "max"), "usd2", "Highest appeal break-even threshold", M),
    ("q7.pwin_min", "econ.json", lambda: econ_rows("p_win", "min"), "pct0", "Lowest payer appeal win rate", S),
    ("q7.pwin_max", "econ.json", lambda: econ_rows("p_win", "max"), "pct0", "Highest payer appeal win rate", S),
    ("q7.above_threshold", "econ.json", "headline.total_shortfall_above_threshold_usd.value", "usd2", "Shortfall on claims above their payer's threshold"),
    ("q7.net_recovery", "econ.json", "headline.total_expected_net_recovery_usd.value", "usd2", "Expected net recovery after appeal costs"),
    ("q7.penalty_min", "econ.json", lambda: econ_rows("deterrence_penalty_multiple", "min"), "f2", "Lowest penalty multiple needed to deter underpricing", M),
    ("q7.penalty_max", "econ.json", lambda: econ_rows("deterrence_penalty_multiple", "max"), "f2", "Highest penalty multiple needed to deter underpricing", M),
    ("q7.bootstrap_reps", "econ.json", "assumptions.bootstrap_reps.value", "int", "Bootstrap replicates per payer"),
    ("q7.p03_planted", "econ_validation.json", "per_payer.rows[payer_id=P03].planted_probability", "pct0", "P03 planted underpayment probability (simulation input)"),
    ("q7.p03_realised", "econ_validation.json", "per_payer.rows[payer_id=P03].realised_share_among_priced_claims", "pct3", "P03 underpayment share realised in the simulated sample"),
    ("q7.patient_at_contract", "econ.json", lambda: gap_reason("AT_CONTRACT", "patient_resp_usd"), "usd2", "Patient share on lines priced exactly at contract (not shortfall)", S),
    ("q7.reduced_usd", "econ.json", lambda: gap_reason("REDUCED_AFTER_PAYMENT", "gap_vs_contract_usd"), "usd2", "Allowed amounts cut by later repricing", M),
    ("q7.reduced_lines", "econ.json", lambda: gap_reason("REDUCED_AFTER_PAYMENT", "n_lines"), "int", "Lines repriced downward after payment", S),
    ("q7.overpaid_usd", "econ.json", lambda: -gap_reason("OVERPAID", "gap_vs_contract_usd"), "usd2", "Overpayment above contract (money the hospital may owe back)", M),
    ("q7.overpaid_lines", "econ.json", lambda: gap_reason("OVERPAID", "n_lines"), "int", "Lines paid above contract", S),
    ("q7.p03_net_recovery", "econ.json", "incentives_by_payer.rows[payer_id=P03].expected_net_recovery_usd", "usd2", "P03 expected net recovery above its threshold"),
    ("q7.p03_alt_recovery", "econ.json", "incentives_by_payer.rows[payer_id=P03].expected_net_recovery_alt_usd", "usd2", "P03 expected net recovery if disputes win at the alternative rate"),
    ("q7.threshold_alt", "econ.json", "incentives_by_payer.rows[payer_id=P03].threshold_alt_usd", "usd2", "Appeal threshold at the alternative dispute win rate"),
    ("q7.p03_n_above", "econ.json", "incentives_by_payer.rows[payer_id=P03].n_above_threshold", "int", "P03 underpaid claims above the threshold"),
    ("q7.p03_above_usd", "econ.json", "incentives_by_payer.rows[payer_id=P03].shortfall_above_threshold_usd", "usd2", "P03 shortfall on claims above the threshold"),
    ("q7.clause_share", "econ.json", "incentives_by_payer.rows[payer_id=P03].interest_clause_as_share_of_s_90d", "pct2", "Interest clause over 90 days as a share of the shortfall"),
    ("q7.float_gain_cents", "econ.json", lambda: 100 * load("econ.json")["incentives_by_payer"]["rows"][0]["payer_float_gain_per_dollar_30d_late"], "f2", "Cents a payer earns per dollar paid 30 days late at SOFR (P01; other payers within 0.01 cent)", M),
    ("q7.clause_cost_cents", "econ.json", lambda: -100 * load("econ.json")["incentives_by_payer"]["rows"][0]["payer_cost_per_dollar_30d_late_with_clause"], "f2", "Cents a payer loses per dollar paid 30 days late under the interest clause (P01)", M),
    ("q7.p03_annual_gain", "econ.json", "incentives_by_payer.rows[payer_id=P03].annual_gain_shortfall_plus_late_float_usd", "usd2", "P03 annual gain from shortfall plus late float"),
    ("q7.p03_renewal_loss", "econ.json", "incentives_by_payer.rows[payer_id=P03].renewal_min_annual_loss_to_deter_usd", "usd2", "Annual renewal loss that deters P03"),
    ("q7.renewal_weight", "econ.json", "assumptions.renewal_discount_factor.value", "f1", "Renewal discount factor (assumption)"),
    ("q7.clause_rate", "econ.json", "assumptions.prompt_pay_interest_rate.value", "pct0", "Illustrative interest clause, per year (assumption)"),
    ("q7.pwin_alt", "econ.json", "assumptions.p_win_dispute_alt.value", "pct0", "Alternative dispute win rate (assumption)"),
    ("q7.p01.share", "econ.json", "underpayment_by_payer.rows[payer_id=P01].underpaid_claim_share", "pct1", "P01 share of priced claims underpaid"),
    ("q7.p01.rate", "econ.json", "underpayment_by_payer.rows[payer_id=P01].shortfall_rate", "pct2", "P01 shortfall rate (shortfall / contract)"),
    ("q7.p01.usd", "econ.json", "underpayment_by_payer.rows[payer_id=P01].shortfall_usd", "usd2", "P01 shortfall dollars"),
    ("q7.p01.usd_lo", "econ.json", "underpayment_by_payer.rows[payer_id=P01].shortfall_usd_lo", "usd2", "P01 shortfall, 95% interval low"),
    ("q7.p01.usd_hi", "econ.json", "underpayment_by_payer.rows[payer_id=P01].shortfall_usd_hi", "usd2", "P01 shortfall, 95% interval high"),
    ("q7.p01.late_per100k", "econ.json", "float_by_payer.rows[payer_id=P01].float_late_per_100k_paid", "usd2", "P01 late float per $100,000 paid"),
    ("q7.p02.share", "econ.json", "underpayment_by_payer.rows[payer_id=P02].underpaid_claim_share", "pct1", "P02 share of priced claims underpaid"),
    ("q7.p02.rate", "econ.json", "underpayment_by_payer.rows[payer_id=P02].shortfall_rate", "pct2", "P02 shortfall rate (shortfall / contract)"),
    ("q7.p02.usd", "econ.json", "underpayment_by_payer.rows[payer_id=P02].shortfall_usd", "usd2", "P02 shortfall dollars"),
    ("q7.p02.usd_lo", "econ.json", "underpayment_by_payer.rows[payer_id=P02].shortfall_usd_lo", "usd2", "P02 shortfall, 95% interval low"),
    ("q7.p02.usd_hi", "econ.json", "underpayment_by_payer.rows[payer_id=P02].shortfall_usd_hi", "usd2", "P02 shortfall, 95% interval high"),
    ("q7.p02.late_per100k", "econ.json", "float_by_payer.rows[payer_id=P02].float_late_per_100k_paid", "usd2", "P02 late float per $100,000 paid"),
    ("q7.p03.share", "econ.json", "underpayment_by_payer.rows[payer_id=P03].underpaid_claim_share", "pct1", "P03 share of priced claims underpaid"),
    ("q7.p03.rate", "econ.json", "underpayment_by_payer.rows[payer_id=P03].shortfall_rate", "pct2", "P03 shortfall rate (shortfall / contract)"),
    ("q7.p03.usd", "econ.json", "underpayment_by_payer.rows[payer_id=P03].shortfall_usd", "usd2", "P03 shortfall dollars"),
    ("q7.p03.usd_lo", "econ.json", "underpayment_by_payer.rows[payer_id=P03].shortfall_usd_lo", "usd2", "P03 shortfall, 95% interval low"),
    ("q7.p03.usd_hi", "econ.json", "underpayment_by_payer.rows[payer_id=P03].shortfall_usd_hi", "usd2", "P03 shortfall, 95% interval high"),
    ("q7.p03.late_per100k", "econ.json", "float_by_payer.rows[payer_id=P03].float_late_per_100k_paid", "usd2", "P03 late float per $100,000 paid"),
    ("q7.p04.share", "econ.json", "underpayment_by_payer.rows[payer_id=P04].underpaid_claim_share", "pct1", "P04 share of priced claims underpaid"),
    ("q7.p04.rate", "econ.json", "underpayment_by_payer.rows[payer_id=P04].shortfall_rate", "pct2", "P04 shortfall rate (shortfall / contract)"),
    ("q7.p04.usd", "econ.json", "underpayment_by_payer.rows[payer_id=P04].shortfall_usd", "usd2", "P04 shortfall dollars"),
    ("q7.p04.usd_lo", "econ.json", "underpayment_by_payer.rows[payer_id=P04].shortfall_usd_lo", "usd2", "P04 shortfall, 95% interval low"),
    ("q7.p04.usd_hi", "econ.json", "underpayment_by_payer.rows[payer_id=P04].shortfall_usd_hi", "usd2", "P04 shortfall, 95% interval high"),
    ("q7.p04.late_per100k", "econ.json", "float_by_payer.rows[payer_id=P04].float_late_per_100k_paid", "usd2", "P04 late float per $100,000 paid"),
    ("q7.p05.share", "econ.json", "underpayment_by_payer.rows[payer_id=P05].underpaid_claim_share", "pct1", "P05 share of priced claims underpaid"),
    ("q7.p05.rate", "econ.json", "underpayment_by_payer.rows[payer_id=P05].shortfall_rate", "pct2", "P05 shortfall rate (shortfall / contract)"),
    ("q7.p05.usd", "econ.json", "underpayment_by_payer.rows[payer_id=P05].shortfall_usd", "usd2", "P05 shortfall dollars"),
    ("q7.p05.usd_lo", "econ.json", "underpayment_by_payer.rows[payer_id=P05].shortfall_usd_lo", "usd2", "P05 shortfall, 95% interval low"),
    ("q7.p05.usd_hi", "econ.json", "underpayment_by_payer.rows[payer_id=P05].shortfall_usd_hi", "usd2", "P05 shortfall, 95% interval high"),
    ("q7.p05.late_per100k", "econ.json", "float_by_payer.rows[payer_id=P05].float_late_per_100k_paid", "usd2", "P05 late float per $100,000 paid"),
    ("q7.p06.share", "econ.json", "underpayment_by_payer.rows[payer_id=P06].underpaid_claim_share", "pct1", "P06 share of priced claims underpaid"),
    ("q7.p06.rate", "econ.json", "underpayment_by_payer.rows[payer_id=P06].shortfall_rate", "pct2", "P06 shortfall rate (shortfall / contract)"),
    ("q7.p06.usd", "econ.json", "underpayment_by_payer.rows[payer_id=P06].shortfall_usd", "usd2", "P06 shortfall dollars"),
    ("q7.p06.usd_lo", "econ.json", "underpayment_by_payer.rows[payer_id=P06].shortfall_usd_lo", "usd2", "P06 shortfall, 95% interval low"),
    ("q7.p06.usd_hi", "econ.json", "underpayment_by_payer.rows[payer_id=P06].shortfall_usd_hi", "usd2", "P06 shortfall, 95% interval high"),
    ("q7.p06.late_per100k", "econ.json", "float_by_payer.rows[payer_id=P06].float_late_per_100k_paid", "usd2", "P06 late float per $100,000 paid"),
    ("q7.appeal_cost", "econ.json", "appeal_cost_usd.value", "usd0", "Cost of one appeal (assumption)"),
    ("q7.prompt_pay_days", "econ.json", "assumptions.prompt_pay_days.value", "int", "Prompt-pay window in days (assumption)"),

    # ---------------- repository ----------------
    ("§", "Repository facts (code, not data)"),
    ("repo.full_run_minutes", "core_build.json", full_run_minutes, "f0", "Approximate minutes for a full run_all (core build + the seven models, from their recorded timings)", CODE_FACT),
    ("repo.tests", "tests/", tests_collected, "int", "Automated tests collected by pytest", CODE_FACT),
    ("repo.test_files", "tests/", lambda: len(list((ROOT / "tests").glob("test_*.py"))), "int", "Test files", CODE_FACT),
    ("repo.sql_files", "sql/", lambda: len(list((ROOT / "sql").glob("*.sql"))) + len(list((ROOT / "sql" / "models").glob("*.sql"))), "int", "SQL files (core + model SQL)", CODE_FACT),
    ("repo.match_rules", "sql/03_reference.sql", match_rules, "int", "Rules in recon.match_rule (matching, duplicate, reversal, variance)", CODE_FACT),
    ("repo.figures", "figures/", lambda: len(list((ROOT / "figures").glob("*.png"))), "int", "Figures written by the models", CODE_FACT),
    ("repo.app_command", "app/", app_command, "str", "Command that starts the Streamlit app", CODE_FACT),
    ("repo.report_pdf", "repository", report_pdf, "str", "Path of the CFO PDF report", CODE_FACT),
    ("repo.app_screenshots", "figures/", app_screenshots, "int", "App screenshots available for the dossier (figures/app_*.png)", CODE_FACT),
]


# ------------------------------------------------------------------------------------------
def evaluate() -> tuple[dict[str, dict], list[tuple[str, str]]]:
    out: dict[str, dict] = {}
    sections: list[tuple[str, str]] = []
    current = ""
    for entry in REGISTRY:
        if entry[0] == "§":
            current = entry[1]
            continue
        key, source, getter, how, meaning, *rest = entry
        label_override = rest[0] if rest else None
        rec = {"key": key, "source": source, "meaning": meaning, "format": how, "section": current}
        try:
            if callable(getter):
                raw = getter()
                label = label_override or S
            else:
                path = getter.replace("\\.", "\x00")
                # escaped dots (file names used as JSON keys) are kept literal
                obj = load(source)
                cur, chain = _walk_escaped(obj, path)
                raw = cur
                label = label_override or auto_label(path.replace("\x00", "."), chain, S)
            rec.update(raw=raw, value=fmt(raw, how), label=label, missing=False)
        except Missing as e:
            rec.update(raw=None, value="{{" + key + "}}", label=label_override or "", missing=True, why=str(e))
        out[key] = rec
        sections.append((current, key))
    return out, sections


def _walk_escaped(obj: Any, path: str) -> tuple[Any, list[Any]]:
    parts = re.split(r"\.(?![^\[]*\])", path)  # split on dots outside [...]
    chain: list[Any] = []
    cur = obj
    for part in parts:
        part = part.replace("\x00", ".")
        sub_path = part
        if "[" in part:
            cur, sub = walk(cur, sub_path)
            chain.extend(sub)
        else:
            chain.append(cur)
            if not isinstance(cur, dict) or part not in cur:
                raise Missing(f"field '{path.replace(chr(0), '.')}' not found ({part})")
            cur = cur[part]
    return cur, chain


def write_table(nums: dict[str, dict]) -> None:
    lines = [
        "# Numbers quoted in the documentation",
        "",
        "_Generated by `scripts/build_docs_numbers.py` from `results/*.json`. Do not edit by hand._",
        "_Every number in README.md, INTERVIEW_GUIDE.md, docs/one_pagers/, docs/career/, practice/ and the project dossier comes from this table._",
        "",
        "All data are simulated for a fictional hospital, six fictional payers, one bank account and one drug wholesaler, "
        "except rows labelled `observed_public` or `published_aggregate`. Labels follow `docs/DESIGN.md` section 8; "
        "`n/a (code fact)` marks facts about the code (tests, files), which are not data.",
        "",
    ]
    missing = [r for r in nums.values() if r["missing"]]
    if missing:
        lines += ["**Waiting for results** (shown as `{{key}}` in the documents until the file exists):", ""]
        lines += [f"- `{r['key']}`: {r['why']}" for r in missing]
        lines.append("")
    current = None
    for r in nums.values():
        if r["section"] != current:
            current = r["section"]
            lines += ["", f"## {current}", "", "| Key | Value | Label | Source | Meaning |", "|---|---|---|---|---|"]
        val = r["value"].replace("|", "\\|")
        lines.append(f"| `{r['key']}` | {val} | {r['label'] or '-'} | `{r['source']}` | {r['meaning']} |")
    OUT_MD.write_text("\n".join(lines) + "\n")
    OUT_JSON.write_text(json.dumps({k: {kk: vv for kk, vv in v.items()} for k, v in nums.items()},
                                   indent=1, default=str) + "\n")


def doc_files() -> list[Path]:
    files: list[Path] = []
    for g in DOC_GLOBS:
        files += [Path(p) for p in glob.glob(str(ROOT / g), recursive=True)]
    return sorted(set(f for f in files if f.is_file()))


def apply(nums: dict[str, dict], write: bool) -> int:
    problems = 0
    for f in doc_files():
        txt = f.read_text()
        unknown, stale, placeholders = [], 0, set()
        n_short = len(SHORT.findall(txt))
        txt = SHORT.sub(lambda m: f"<!--n:{m.group(1)}-->(new)<!--/n-->", txt)
        stale += 0

        def sub(m: re.Match) -> str:
            nonlocal stale
            k, old = m.group(1), m.group(2)
            if k not in nums:
                unknown.append(k)
                return m.group(0)
            new = nums[k]["value"]
            if nums[k]["missing"]:
                placeholders.add(k)
            if new != old:
                stale += 1
            return f"<!--n:{k}-->{new}<!--/n-->"

        new_txt = MARKER.sub(sub, txt)
        rel = f.relative_to(ROOT)
        fenced = False
        for ln_no, ln in enumerate(new_txt.splitlines(), 1):
            if ln.lstrip().startswith("```"):
                fenced = not fenced
            elif fenced and "<!--n:" in ln and f.suffix == ".md" and "project_dossier" not in f.parts:
                print(f"WARN  {rel}:{ln_no}: marker inside a code block renders as raw text on GitHub")
        if unknown:
            print(f"ERROR {rel}: unknown keys {sorted(set(unknown))}")
            problems += 1
        if placeholders:
            print(f"note  {rel}: placeholders waiting for results: {sorted(placeholders)}")
            if not write:
                problems += 1
        if n_short and not write:
            print(f"STALE   {rel}: {n_short} unexpanded {{{{n:key}}}} shorthand(s)")
            problems += 1
        if stale:
            print(f"{'updated' if write else 'STALE  '} {rel}: {stale} number(s)")
            if write:
                f.write_text(new_txt)
            else:
                problems += 1
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true", help="rewrite markers in the documentation files")
    ap.add_argument("--check", action="store_true", help="exit 1 if any document marker is stale or unknown")
    a = ap.parse_args()
    nums, _ = evaluate()
    write_table(nums)
    n_missing = sum(r["missing"] for r in nums.values())
    print(f"wrote {OUT_MD.relative_to(ROOT)}: {len(nums)} numbers, {n_missing} waiting for results")
    if a.apply or a.check:
        problems = apply(nums, write=a.apply and not a.check)
        if a.check and problems:
            return 1
        if a.apply and problems:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
