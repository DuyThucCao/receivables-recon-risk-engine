"""Small throw-away build: the pipeline never opens ground truth, and the intake helper
reconciles a new remittance + bank statement + manual claim incrementally."""
import shutil
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from recon_engine import db, pipeline  # noqa: E402
from recon_engine.datagen import generate  # noqa: E402

DBNAME = "rre_test_small"
PIPELINE_SOURCES = [ROOT / "src" / "recon_engine" / f for f in ("pipeline.py", "db.py", "labels.py")] + \
    sorted((ROOT / "sql").glob("*.sql"))
MODEL_SOURCES = sorted((ROOT / "src" / "recon_engine" / "models").glob("*.py")) + sorted((ROOT / "sql" / "models").glob("*.sql"))
TRUTH_ACCESS = ("data/truth", "TRUTH_DIR", "planted_parameters.json", "_truth.csv", "datagen.generate", "datagen.evaluate")


@pytest.fixture(scope="module")
def small(tmp_path_factory):
    base = tmp_path_factory.mktemp("small")
    raw, truth = base / "raw", base / "truth"
    generate(raw, truth, n_claims=600)
    opened, raw_opened = [], []

    def hook(event, args):
        if event == "open" and args and isinstance(args[0], (str, bytes, Path)):
            p = str(args[0] if not isinstance(args[0], bytes) else args[0].decode())
            if str(truth) in p or "/data/truth" in p:
                opened.append(p)
            elif str(raw) in p:
                raw_opened.append(p)

    sys.addaudithook(hook)
    db.drop_database(DBNAME)
    info = pipeline.build(raw, database=DBNAME, run_params={"as_of_date": "2026-03-31"}, verbose=False)
    seen, seen_raw = list(opened), list(raw_opened)
    conn = db.connect(DBNAME)
    yield {"raw": raw, "truth": truth, "info": info, "opened": seen, "raw_opened": seen_raw, "conn": conn}
    conn.close()
    db.drop_database(DBNAME)
    shutil.rmtree(base, ignore_errors=True)


def test_pipeline_sources_never_mention_truth():
    for p in PIPELINE_SOURCES:
        assert "truth" not in p.read_text().lower(), p


def test_model_sources_have_no_truth_paths():
    """Model modules (other workstreams) may discuss recovery of planted values in comments, but must
    not reference the ground-truth folder, files or the generator from their code."""
    import io
    import tokenize
    for p in MODEL_SOURCES:
        text = p.read_text()
        if p.suffix == ".py":   # ignore comments
            toks = tokenize.generate_tokens(io.StringIO(text).readline)
            text = " ".join(t.string for t in toks if t.type != tokenize.COMMENT)
        else:                   # ignore SQL line comments
            text = "\n".join(line.split("--", 1)[0] for line in text.splitlines())
        assert not [t for t in TRUTH_ACCESS if t in text], p


def test_pipeline_never_opens_truth_at_runtime(small):
    assert len(small["raw_opened"]) >= 7, "audit hook did not observe the raw files being read"
    assert small["opened"] == []


def test_small_build_ties_and_closes(small):
    c = small["conn"]
    assert c.execute("select bool_and(passed) from recon.control_totals").fetchone()[0]
    assert small["info"]["close"]["refused"] is None


def test_rebuild_is_idempotent(small):
    c = small["conn"]
    before = c.execute("select count(*), sum(paid), sum(open_balance) from recon.claim_line_balance").fetchone()
    c.close()
    pipeline.build(small["raw"], database=DBNAME, run_params={"as_of_date": "2026-03-31"}, verbose=False, close=False)
    small["conn"] = c = db.connect(DBNAME)
    after = c.execute("select count(*), sum(paid), sum(open_balance) from recon.claim_line_balance").fetchone()
    assert before == after


def test_intake_matches_new_remittance_and_deposit(small):
    c = small["conn"]
    pipeline.close_days(c)
    line = c.execute("""select c.claim_id, c.line_no, c.line_item_control_no, c.member_id_norm, c.service_date,
                               c.procedure_code, c.units, c.line_charge, c.payer_id
                        from core.fact_claim_line c join recon.claim_line_status s using (claim_line_id)
                        where s.status = 'SUBMITTED' and c.payer_id = 'P01' order by c.claim_line_id limit 1""").fetchone()
    assert line, "need an open P01 line"
    cid, ln, lic, member, sdate, proc, units, charge, payer = line
    paid = round(float(charge) * 0.5, 2)
    ca = round(float(charge) - paid, 2)
    remit = pd.DataFrame([{
        "remit_file_id": "ERA_TEST_1", "payer_id": payer, "payer_name": "ALDER RIDGE HEALTH PLAN", "trace_no": "7000000001",
        "payment_method": "ACH", "payment_date": "20260406", "file_payment_amount": f"{paid:.2f}",
        "patient_control_no": cid, "payer_claim_control_no": "TESTICN1", "claim_status_code": "1",
        "member_id": member, "service_date": sdate.strftime("%Y%m%d"), "procedure_code": proc, "units": str(units),
        "line_item_control_no": lic, "line_charge_amount": f"{charge}", "line_allowed_amount": f"{paid:.2f}",
        "line_paid_amount": f"{paid:.2f}", "adj_group_1": "CO", "adj_reason_1": "45", "adj_amount_1": f"{ca:.2f}"},
        {"remit_file_id": "ERA_TEST_1", "payer_id": payer, "payer_name": "ALDER RIDGE HEALTH PLAN", "trace_no": "7000000001",
         "payment_method": "ACH", "payment_date": "20260406", "file_payment_amount": f"{paid:.2f}",
         "patient_control_no": "XYZ0000001", "payer_claim_control_no": "TESTICN2", "claim_status_code": "1",
         "member_id": "NOBODY1", "service_date": "20260301", "procedure_code": "99213", "units": "1",
         "line_item_control_no": "", "line_charge_amount": "100", "line_allowed_amount": "0", "line_paid_amount": "0",
         "adj_group_1": "CO", "adj_reason_1": "45", "adj_amount_1": "100"}])
    last_bal = c.execute("select running_balance from core.fact_bank_txn order by posting_date desc, bank_txn_id desc limit 1").fetchone()[0]
    bank = pd.DataFrame([{"account_number": "XXXXXX4821", "posting_date": "04/06/2026", "value_date": "04/06/2026",
                          "transaction_type": "ACH CREDIT",
                          "description": "ACH CREDIT ALDER RIDGE HP HCCLAIMPMT TRN*1*7000000001*1900000101\\",
                          "debit": "", "credit": f"{paid:.2f}", "running_balance": f"{float(last_bal) + paid:.2f}",
                          "bank_reference": "FHBTEST000001", "customer_reference": ""}])
    claim = {"claim_id": "MHS269999999", "form_type": "CMS1500", "payer_id": "P01", "payer_name": "Alder Ridge Health Plan",
             "member_id": member, "patient_account_no": "A00000001", "service_line": "PRIMARY_CARE",
             "submission_date": "2026-04-02", "claim_total_charge": "180.00", "line_no": "1",
             "line_item_control_no": "MHS269999999-01", "procedure_code": "99213", "service_date": "04/01/2026",
             "units": "1", "line_charge": "180.00"}
    res = pipeline.ingest_and_reconcile(c, remittance=remit, bank_statement=bank, claim=claim, batch_label="pytest")
    cm = res["claim_matches"]
    hit = cm[cm["patient_control_no_raw"] == cid]
    assert hit["rule_id"].tolist() == ["CR10_EXACT_CLAIM_ID"]
    assert hit["line_status"].tolist() == ["PARTIAL"] or hit["line_status"].tolist() == ["PAID"]
    bm = res["bank_matches"]
    assert bm["rule_id"].tolist() == ["RB10_TRACE_EXACT"]
    types = set(res["new_exceptions"]["exception_type"])
    assert "UNMATCHED_REMIT" in types
    assert c.execute("select count(*) from core.fact_claim_line where claim_id = 'MHS269999999'").fetchone()[0] == 1
    assert c.execute("select bool_and(passed) from recon.control_totals").fetchone()[0]
    assert res["close"]["refused"] is None


def test_intake_rejects_are_reported(small):
    c = small["conn"]
    bad = {"claim_id": "MHS269999998", "payer_id": "P01", "line_no": "1", "procedure_code": "", "service_date": "04/01/2026",
           "submission_date": "2026-04-02", "line_charge": "50.00"}
    res = pipeline.ingest_and_reconcile(c, claim=bad, batch_label="pytest_bad", close=False)
    assert res["rejects"]["reason_code"].tolist() == ["MISSING_PROCEDURE_CODE"]
    assert "STAGING_REJECT" in set(res["new_exceptions"]["exception_type"])
