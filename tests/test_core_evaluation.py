"""Matching quality against planted ground truth, and rejects counted against planted bad rows.

Tests may read data/truth (pipeline code may not). Thresholds are regression guards for this
synthetic data set, not claims about real-world performance.
"""
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from recon_engine import db  # noqa: E402
from recon_engine.datagen.evaluate import run_evaluation  # noqa: E402

TRUTH = ROOT / "data" / "truth"


@pytest.fixture(scope="module")
def ev(tmp_path_factory):
    if not (TRUTH / "remit_line_truth.csv").exists():
        pytest.skip("no ground truth: run `python run_all.py` first")
    with db.connect() as conn:
        if not conn.execute("select to_regclass('recon.control_totals')").fetchone()[0]:
            pytest.skip("database not built")
        return run_evaluation(conn, results_dir=tmp_path_factory.mktemp("eval"))


def test_results_file_written_by_run_all():
    p = ROOT / "results" / "reconciliation.json"
    assert p.exists()
    r = json.loads(p.read_text())
    assert r["label"] == "synthetic_record"
    assert {"per_rule", "by_case", "missed_postable_lines", "wrong_matches"} <= set(r["claim_to_remittance"])
    assert r["what_the_rules_cannot_resolve"]


def test_tie_out_reported(ev):
    assert ev["tie_out"]["all_control_totals_passed"]
    assert ev["tie_out"]["max_abs_difference"] == 0.0


def test_claim_rules_precision(ev):
    for rule, v in ev["claim_to_remittance"]["per_rule"].items():
        assert v["precision"] >= 0.99, (rule, v)


def test_claim_recall_by_case(ev):
    cases = ev["claim_to_remittance"]["by_case"]
    for d in ("none", "padding", "prefix", "punctuation", "lowercase", "transposed", "repeated_service_with_line_control"):
        assert cases[d]["recall"] >= 0.99, (d, cases[d])
    assert set(cases["transposed"]["rules_used"]) <= {"CR30_MEMBER_DATE_AMOUNT"}
    # the genuinely ambiguous case is queued, not guessed
    twin = cases["transposed_id_with_twin_claim"]
    assert twin["matched_wrong"] == 0
    assert twin["of_which_ambiguous"] > 0


def test_every_case_accounted(ev):
    for k, v in ev["claim_to_remittance"]["by_case"].items():
        assert v["matched_correct"] + v["matched_wrong"] + v["wrongly_excluded_as_duplicate"] + v["sent_to_exception_queue"] <= v["lines"]
        assert v["matched_correct"] + v["matched_wrong"] + v["wrongly_excluded_as_duplicate"] + v["sent_to_exception_queue"] >= v["lines"] - 1, (k, v)


def test_duplicates_and_orphans(ev):
    dd = ev["claim_to_remittance"]["duplicate_detection"]
    assert dd["precision"] >= 0.99 and dd["recall"] >= 0.99
    assert ev["claim_to_remittance"]["orphans_from_other_facility"]["wrongly_matched"] == 0
    assert ev["claim_to_remittance"]["provider_level_rows"]["wrongly_matched_to_claims"] == 0


def test_bank_matching(ev):
    rb = ev["remittance_to_bank"]
    for rule, v in rb["per_rule"].items():
        assert v["precision"] >= 0.99, (rule, v)
    for cat in ("deposit_missing", "deposit_after_as_of"):
        if cat in rb["by_case"]:
            assert rb["by_case"][cat]["wrongly_matched"] == 0
    assert rb["unapplied_cash"]["planted_and_queued"] == rb["unapplied_cash"]["planted_deposits_without_remittance"]
    assert rb["by_case"]["lockbox_bundle"]["recall"] >= 0.95
    untraced = rb["by_case"]["equal_amount_provider_payment_untraced"]
    assert untraced["matched_wrong"] == 0 and untraced["of_which_ambiguous"] > 0
    split = rb["by_case"].get("split_deposit_second_untraced")
    if split:
        assert split["matched_wrong"] == 0 and split["sent_to_exception_queue"] == split["payments"]


def test_rejects_are_counted_not_dropped(ev):
    for f, v in ev["staging_rejects_vs_planted"].items():
        assert v["planted_and_rejected"] == v["planted_bad_rows"], (f, v)
        assert v["rejected_not_planted"] == 0, (f, v)
    with db.connect() as conn:
        rows = conn.execute("select check_name, scope, passed from recon.control_totals where check_name = 'RAW_ROWS_ACCOUNTED'").fetchall()
        assert len(rows) == 7 and all(r[2] for r in rows)
        assert conn.execute("select count(*) from stg.reject where reason_code is null or raw_record is null").fetchone()[0] == 0
        # every reject is visible in the exception queue
        n_rej = conn.execute("select count(*) from stg.reject").fetchone()[0]
        n_q = conn.execute("select count(*) from recon.exception_queue where exception_type = 'STAGING_REJECT' and status = 'OPEN'").fetchone()[0]
        assert n_rej == n_q


def test_orphan_reversals_explained(ev):
    assert ev["orphan_reversals"]["unexplained"] == 0


def test_planted_markov_rows_are_stochastic():
    p = json.loads((TRUTH / "planted_parameters.json").read_text())
    for payer, m in p["markov_by_payer"].items():
        for row in m["population_matrix"]:
            assert abs(sum(row) - 1) < 1e-6
    t = pd.read_csv(TRUTH / "claim_truth.csv")
    assert set(t["final_state_ultimate"]) <= {"PAID", "WRITTEN_OFF"}
