"""Streamlit app: modules import, every screen's data loads from the built database, the
reconciliation desk round trip moves the CFO numbers and Reset restores the baseline, and every
screen renders without an exception (streamlit.testing.v1.AppTest).

Database tests use the pipeline database named by RRE_PG_DIR / RRE_DB_NAME (as the other tests do)
and their own throw-away demo copy (``<baseline>_apptest``), so a running app is not disturbed.
They skip when no built database is reachable.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
sys.path.insert(0, str(APP))
sys.path.insert(0, str(ROOT / "src"))

VIEWS = ["desk", "cfo", "revcycle", "treasurer", "actuary", "credit", "pharmacy", "economist", "methods"]


def test_app_modules_import():
    for m in ["rre_app", "rre_app.paths", "rre_app.demo_db", "rre_app.loaders", "rre_app.batches", "rre_app.ui",
              "rre_app.state"] + [f"rre_app.views.{v}" for v in VIEWS]:
        mod = importlib.import_module(m)
        assert mod is not None
    from rre_app import views  # noqa: F401
    for v in VIEWS:
        mod = importlib.import_module(f"rre_app.views.{v}")
        assert callable(mod.render) and mod.TITLE and mod.QUESTION and mod.JOB


def test_every_screen_title_names_a_job_and_a_question():
    for v in VIEWS:
        mod = importlib.import_module(f"rre_app.views.{v}")
        assert mod.QUESTION.endswith("?")


def test_manual_claim_rows_match_raw_export_format():
    from datetime import date

    from rre_app import batches
    df = batches.manual_claim_rows(claim_id="MHS269999999", payer_id="P01", payer_name="Alder Ridge Health Plan",
                                   member_id="X1", service_line="EMERGENCY", service_date=date(2026, 3, 30),
                                   submission_date=date(2026, 3, 31),
                                   lines=[{"procedure_code": "99283", "revenue_code": "0450", "units": 1, "line_charge": 450},
                                          {"procedure_code": "85025", "revenue_code": "0305", "units": 2, "line_charge": 110}])
    assert list(df.columns) == batches.CLAIM_COLUMNS
    assert df["claim_total_charge"].unique().tolist() == ["560.00"]
    assert df["line_item_control_no"].tolist() == ["MHS269999999-01", "MHS269999999-02"]


# ------------------------------------------------------------------ database fixtures
@pytest.fixture(scope="module")
def demo():
    """A demo copy just for these tests (created from the pipeline database, dropped at the end)."""
    from recon_engine import db
    old = os.environ.get("RRE_DEMO_DB")
    os.environ["RRE_DEMO_DB"] = f"{db.db_name()}_apptest"
    from rre_app import demo_db
    if demo_db.hosted():
        pytest.skip("DATABASE_URL is set: database copies are not available on a hosted server")
    try:
        demo_db.recreate_demo()
    except Exception as e:  # noqa: BLE001
        if old is None:
            os.environ.pop("RRE_DEMO_DB", None)
        pytest.skip(f"no built database reachable: {e}")
    yield demo_db
    try:
        db.drop_database(demo_db.demo_name())
    finally:
        if old is None:
            os.environ.pop("RRE_DEMO_DB", None)
        else:
            os.environ["RRE_DEMO_DB"] = old


@pytest.fixture()
def conn(demo):
    c = demo.connect()
    yield c
    c.close()


def _results(name):
    p = ROOT / "results" / f"{name}.json"
    return json.loads(p.read_text()) if p.exists() else None


# ------------------------------------------------------------------ loaders
@pytest.mark.parametrize("name", VIEWS)
def test_every_loader_runs_against_the_built_database(conn, name):
    from rre_app import loaders
    out = loaders.LOADERS[name](conn)
    assert isinstance(out, dict)
    if "missing" in out:
        assert isinstance(out["missing"], list)


def test_q1_marts_are_present_and_tie(conn):
    from rre_app import loaders
    d = loaders.load_cfo(conn)
    assert d["missing"] == []
    billed = float(conn.execute("select sum(billed) from recon.claim_line_balance").fetchone()[0])
    assert d["totals"]["billed"] == pytest.approx(billed, abs=0.01)
    assert set(d["cash_stuck"]["bucket"]) >= {"NOT_YET_ADJUDICATED", "UNAPPLIED_CASH", "DENIED_IN_PLAY"}


def test_worklist_slider_uses_the_model_and_reproduces_the_pipeline(conn):
    from rre_app import loaders
    d = loaders.load_revcycle(conn)
    if "queue" not in d:
        pytest.skip("mart.work_queue not built")
    sol = loaders.solve_worklist(d["queue"], d["staff_hours_default"])
    res = _results("prioritize")
    if res:
        pol = res["todays_queue"]["policies_same_budget"]
        for k, v in pol.items():
            assert sol["policies"][k]["n_worked"] == v["n_worked"]
            assert sol["policies"][k]["expected_net"] == pytest.approx(v["expected_net"], abs=0.05 * max(1, sol["policies"][k]["n_worked"]))
    # more hours never lowers the knapsack's value; the knapsack is never beaten by the simple rules
    small = loaders.solve_worklist(d["queue"], 2.0)["policies"]
    big = loaders.solve_worklist(d["queue"], 40.0)["policies"]
    assert big["MODEL_KNAPSACK"]["expected_net"] >= small["MODEL_KNAPSACK"]["expected_net"]
    for p in (small, big):
        assert p["MODEL_KNAPSACK"]["expected_net"] >= p["OLDEST_FIRST"]["expected_net"] - 1e-6
        assert p["MODEL_KNAPSACK"]["expected_net"] >= p["LARGEST_FIRST"]["expected_net"] - 1e-6


def test_borrowing_base_recompute_matches_the_mart_at_model_terms(conn):
    from rre_app import loaders
    d = loaders.load_credit(conn)
    if "base" not in d:
        pytest.skip("mart.borrowing_base not built")
    A = d["assumptions"]
    for cert in d["certificate_dates"]:
        lines = loaders.credit_lines(conn, cert)
        base = d["base"][d["base"]["certificate_date"].astype(str) == cert]
        t = loaders.recompute_borrowing_base(lines, base, A["advance_rate"]["value"], A["concentration_limit"]["value"],
                                             A["eligibility_max_age_days"]["value"], A["cross_age_pct"]["value"])
        m = base.set_index("payer_id")
        t = t.set_index("payer_id")
        for col in ("gross_ncv", "eligible", "availability"):
            assert t[col].sum() == pytest.approx(m[col].sum(), rel=1e-5, abs=5.0), (cert, col)  # line NCV is stored to the cent
        # a lower advance rate never raises availability
        t2 = loaders.recompute_borrowing_base(lines, base, 0.6, A["concentration_limit"]["value"],
                                              A["eligibility_max_age_days"]["value"], A["cross_age_pct"]["value"])
        assert t2["availability"].sum() <= t["availability"].sum() + 1e-6


def test_cash_simulation_reproduces_results_and_shortfall_moves_with_need(conn):
    from rre_app import loaders
    d = loaders.load_treasurer(conn)
    if "forecast" not in d:
        pytest.skip("mart.cash_forecast not built")
    ss = loaders.steady_state_date()
    sim = loaders.simulate_receipts(conn, ss)
    res = _results("cashflow")
    if res and loaders.val(res.get("shortfall")) and loaders.val(res["shortfall"]).get("forecast_date") == ss:
        exp = loaders.val(res["shortfall"])
        assert float(sim["sims"].sum(axis=1).mean()) == pytest.approx(exp["receipts_13w_mean"], rel=1e-6)
        assert sim["median_receipts"] == pytest.approx(exp["median_weekly_receipts_trailing_26w"], abs=0.01)
    lo = loaders.shortfall(sim, 0.5 * sim["median_receipts"], 1e6)
    hi = loaders.shortfall(sim, 1.5 * sim["median_receipts"], 1e6)
    assert hi["p_shortfall"] >= lo["p_shortfall"]
    assert hi["shortfall_cvar95"] >= lo["shortfall_cvar95"]


def test_appeal_policy_uses_econ_threshold(conn):
    from rre_app import loaders
    d = loaders.load_economist(conn)
    if "claims" not in d or "econ_appeal_outcome" not in d:
        pytest.skip("Q7 marts not built")
    pol = loaders.appeal_policy(d["claims"], d["econ_appeal_outcome"], d["appeal_cost_default"])
    inc = d.get("underpayment_incentive")
    if inc is not None:
        m = inc.set_index("payer_id")
        for r in pol.itertuples():
            assert r.threshold_usd == pytest.approx(float(m.loc[r.payer_id, "threshold_usd"]), abs=0.01)
            assert r.expected_net_recovery_usd == pytest.approx(float(m.loc[r.payer_id, "expected_net_recovery_usd"]), abs=0.05)
    dearer = loaders.appeal_policy(d["claims"], d["econ_appeal_outcome"], 4 * d["appeal_cost_default"])
    assert (dearer["threshold_usd"].to_numpy() > pol["threshold_usd"].to_numpy()).all()


def test_missing_marts_are_reported_not_raised(conn):
    """A model that has not run yet must give a 'not built' notice, never a crash."""
    from rre_app import loaders
    conn.execute("begin")
    try:
        for t in ("mart.cash_forecast", "mart.cash_forecast_backtest", "mart.borrowing_base", "mart.borrowing_base_line",
                  "mart.borrowing_base_stress", "mart.work_queue", "mart.dev_triangle"):
            conn.execute(f"drop table if exists {t} cascade")
        assert "mart.cash_forecast" in loaders.load_treasurer(conn)["missing"]
        assert "mart.borrowing_base" in loaders.load_credit(conn)["missing"]
        assert "mart.work_queue" in loaders.load_revcycle(conn)["missing"]
        assert "mart.dev_triangle" in loaders.load_actuary(conn)["missing"]
    finally:
        conn.rollback()
    assert loaders.load_treasurer(conn)["missing"] == []


# ------------------------------------------------------------------ reconciliation desk round trip
def test_desk_round_trip_and_reset(demo):
    from recon_engine import pipeline
    from rre_app import batches, loaders

    demo.recreate_demo()
    with demo.connect() as c:
        base_snap = loaders.desk_snapshot(c)
        base_cfo = loaders.load_cfo(c)
        assert base_snap["all_passed"]
        b = batches.fresh_batch(c, n_claims=4, seed=7)
        assert list(b.remittance.columns) == batches.REMIT_COLUMNS and list(b.bank.columns) == batches.BANK_COLUMNS
        r = pipeline.ingest_and_reconcile(c, remittance=b.remittance, bank_statement=b.bank, batch_label="apptest")
        after = loaders.desk_snapshot(c)
        after_cfo = loaders.load_cfo(c)
    cm, bm, ex = r["claim_matches"], r["bank_matches"], r["new_exceptions"]
    assert r["landed"] == {"raw.remittance_advice": len(b.remittance), "raw.bank_statement": len(b.bank)}
    assert len(r["rejects"]) == 0
    rules = set(cm["rule_id"].dropna())
    assert {"CR10_EXACT_CLAIM_ID", "CR20_NORMALISED_CLAIM_ID"} <= rules
    assert "RB10_TRACE_EXACT" in set(bm["rule_id"].dropna())
    assert {"UNMATCHED_REMIT", "UNDERPAYMENT", "UNAPPLIED_CASH"} <= set(ex["exception_type"])
    assert r["close"]["refused"] is None
    assert after["all_passed"], after["controls"][~after["controls"]["passed"]]
    # the CFO numbers moved by exactly the cash posted to claims in this batch
    posted = float(cm.loc[cm["rule_id"].notna(), "paid_amount"].astype(float).sum())
    assert after_cfo["totals"]["payer_paid"] - base_cfo["totals"]["payer_paid"] == pytest.approx(posted, abs=0.01)
    unap = after["cash_stuck"].set_index("bucket")["amount"]["UNAPPLIED_CASH"] - \
        base_snap["cash_stuck"].set_index("bucket")["amount"]["UNAPPLIED_CASH"]
    assert unap == pytest.approx(float(b.bank["credit"].iloc[1].replace(",", "")), abs=0.01)
    cmp = loaders.compare_snapshots(base_snap, after)
    assert (cmp.loc[cmp["item"] == "Claim lines: billed", "change"].abs() < 0.01).all()
    # reset restores the baseline exactly
    demo.recreate_demo()
    with demo.connect() as c:
        back = loaders.desk_snapshot(c)
        assert loaders.intake_history(c).empty
    pd.testing.assert_frame_equal(back["cash_stuck"], base_snap["cash_stuck"])
    assert back["identity"] == base_snap["identity"]
    assert back["days_closed"] == base_snap["days_closed"]


def test_desk_remittance_first_then_bank(demo):
    """A remittance alone waits as IN_TRANSIT; its deposit arriving later clears it."""
    from recon_engine import pipeline
    from rre_app import batches

    demo.recreate_demo()
    with demo.connect() as c:
        b = batches.fresh_batch(c, n_claims=2, seed=11, include_defects=False)
        r1 = pipeline.ingest_and_reconcile(c, remittance=b.remittance, batch_label="apptest_r")
        assert "IN_TRANSIT" in set(r1["new_exceptions"]["exception_type"])
        pipeline.ingest_and_reconcile(c, bank_statement=b.bank, batch_label="apptest_b")
        st = c.execute("select status from recon.exception_queue where exception_key = %s",
                       (f"IN_TRANSIT:{b.payer_id}:{b.trace_no}",)).fetchone()
        assert st is not None and st[0] != "OPEN"
        assert c.execute("select bool_and(passed) from recon.control_totals").fetchone()[0]
    demo.recreate_demo()


def test_desk_manual_claim_and_replay(demo):
    from datetime import date

    from recon_engine import pipeline
    from rre_app import batches, loaders

    demo.recreate_demo()
    with demo.connect() as c:
        before = loaders.desk_snapshot(c)
        cid = batches.next_claim_id(c)
        rows = batches.manual_claim_rows(claim_id=cid, payer_id="P02", payer_name="Corvane Mutual Insurance Company",
                                         member_id="DEMO1", service_line="EMERGENCY", service_date=batches.as_of_date(c),
                                         submission_date=batches.as_of_date(c),
                                         lines=[{"procedure_code": "99283", "revenue_code": "0450", "units": 1, "line_charge": 450}])
        pipeline.ingest_and_reconcile(c, claim=rows, batch_label="apptest_claim")
        after = loaders.desk_snapshot(c)
        assert after["identity"]["billed"] - before["identity"]["billed"] == pytest.approx(450.0)
        nya = lambda s: s["cash_stuck"].set_index("bucket").loc["NOT_YET_ADJUDICATED", "n_items"]  # noqa: E731
        assert nya(after) == nya(before) + 1
        # a fresh batch pays the hand-entered claim first
        b = batches.fresh_batch(c, n_claims=1, seed=3, include_defects=False)
        assert b.remittance["patient_control_no"].iloc[0] == cid
        # replaying rows already received posts nothing new
        rr, rb = batches.replay_samples(8)
        paid0 = after["identity"]["paid"]
        r = pipeline.ingest_and_reconcile(c, remittance=rr, bank_statement=rb, batch_label="apptest_replay")
        assert set(r["rejects"]["reason_code"]) == {"DUPLICATE_BANK_REFERENCE"}
        assert loaders.desk_snapshot(c)["identity"]["paid"] == pytest.approx(paid0, abs=0.001)
    demo.recreate_demo()


# ------------------------------------------------------------------ AppTest: every screen renders
def _script(view: str) -> str:
    return (f"import sys\nsys.path.insert(0, {str(APP)!r})\nfrom rre_app import ui\nfrom rre_app.views import {view}\n"
            f"ui.page_css()\n{view}.render()\n")


@pytest.mark.parametrize("view", VIEWS)
def test_screen_renders_without_exception(demo, view):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_string(_script(view), default_timeout=240)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert not at.error, [e.value for e in at.error]
    assert len(at.title) == 1
    text = " ".join(m.value for m in at.markdown) + " ".join(c.value for c in at.caption)
    if view != "methods":
        assert "simulated" in text.lower()


def test_full_app_entry_point_runs(demo):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(APP / "streamlit_app.py"), default_timeout=240)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.title[0].value.startswith("Did today's payments post")


def test_desk_sample_batch_through_the_ui(demo):
    """Click through the desk: generate a fresh batch, run it, see results; then reset."""
    from streamlit.testing.v1 import AppTest

    demo.recreate_demo()
    at = AppTest.from_string(_script("desk"), default_timeout=240)
    at.run()
    next(b for b in at.button if b.label == "Generate a fresh batch").click().run()
    assert not at.exception
    next(b for b in at.button if b.label == "Run remittance + bank").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert any(s.value == "Result of the last run" for s in at.subheader)
    assert any("control totals tie to the cent" in s.value for s in at.success)
    next(b for b in at.button if b.label == "Reset demo database").click().run()
    assert not at.exception
    with demo.connect() as c:
        from rre_app import loaders
        assert loaders.intake_history(c).empty
