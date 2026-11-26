"""Q5 borrowing base (models/credit.py): eligibility waterfall, cross-age and concentration rules on
hand-built books, expected remaining days, the point-in-time tie-out, no look-ahead and
determinism.

Database tests skip when no built database is reachable and roll back everything they change.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recon_engine.models import credit, markov  # noqa: E402


def _book():
    return pd.DataFrame({
        "payer_id":     ["A", "A", "A", "B", "B", "C", "C"],
        "claim_state":  ["IN_REVIEW", "DENIED", "APPEALED", "IN_REVIEW", "IN_REVIEW", "SUBMITTED", "PENDED"],
        "age_days":     [30, 40, 50, 200, 20, 130, 10],
        "ncv":          [100.0, 50.0, 30.0, 80.0, 20.0, 40.0, 10.0],
        "open_balance": [300.0, 0.0, 0.0, 200.0, 50.0, 100.0, 30.0],
    })


def test_eligibility_waterfall_first_failing_rule():
    r = credit.eligibility(_book(), max_age=120, cross_age_pct=0.5)
    # B: 80 of 100 NCV is aged (80% > 50%) -> its young line is CROSS_AGED; C: 40 of 40 aged-or-eligible
    assert r.tolist() == ["ELIGIBLE", "DENIED", "DISPUTED", "AGED", "CROSS_AGED", "AGED", "DISPUTED"]


def test_cross_age_threshold_is_strict():
    b = _book()
    b.loc[3, "ncv"] = 20.0            # B now 20 aged of 40 = exactly 50%: not more than the limit
    r = credit.eligibility(b, max_age=120, cross_age_pct=0.5)
    assert r[4] == "ELIGIBLE"


def test_concentration_fixed_point_by_hand():
    e = pd.Series({"A": 600.0, "B": 200.0, "C": 200.0, "D": 100.0})
    capped = credit.concentration_cap(e, 0.30, tol=1e-9)
    E = capped.sum()
    # A capped at 0.3E, others unchanged: E = 0.3E + 500 -> E = 500/0.7
    assert E == pytest.approx(500 / 0.7, rel=1e-6)
    assert capped["A"] == pytest.approx(0.3 * E, rel=1e-6)
    assert (capped / E).max() <= 0.30 + 1e-9
    assert capped[["B", "C", "D"]].tolist() == [200.0, 200.0, 100.0]


def test_concentration_single_payer_book_is_unusable():
    capped = credit.concentration_cap(pd.Series({"A": 1000.0}), 0.30, tol=1e-6)
    assert capped.sum() == pytest.approx(0.0, abs=1e-3)


def test_borrowing_base_table_and_availability():
    b = _book()
    b.loc[7] = ["A", "IN_REVIEW", 10, 0.0, -25.0]          # a credit balance owed back to payer A
    b["reason"] = credit.eligibility(b, 120, 0.5)
    t = credit.borrowing_base_table(b, advance_rate=0.85, limit=1.0)
    assert t.loc["A", "eligible"] == pytest.approx(100.0)
    assert t.loc["A", "credit_balance_reserve"] == pytest.approx(25.0)
    assert t.loc["A", "availability"] == pytest.approx(0.85 * 100 - 25)
    assert t["gross_ncv"].sum() == pytest.approx(b["ncv"].sum())
    parts = t[["inel_denied", "inel_disputed", "inel_aged", "inel_cross_aged", "eligible_pre_cap"]].sum(axis=1)
    assert np.allclose(parts, t["gross_ncv"])                 # the waterfall adds up


def test_expected_remaining_days():
    cdf = np.zeros(101)
    cdf[10:] = 0.5
    cdf[30:] = 1.0                    # half paid at day 10, half at day 30
    assert credit.expected_remaining_days(cdf, 0) == pytest.approx(0.5 * 10 + 0.5 * 30)
    assert credit.expected_remaining_days(cdf, 15) == pytest.approx(15.0)   # only the day-30 half is left


# ------------------------------------------------------------------ database tests
@pytest.fixture(scope="module")
def conn():
    from recon_engine import db
    try:
        c = db.connect()
        n = c.execute("select count(*) from recon.claim_line_balance").fetchone()[0]
    except Exception as e:  # pragma: no cover
        pytest.skip(f"no built database reachable: {e}")
    if not n:
        pytest.skip("database has no reconciled data")
    markov.ensure_sql(c)
    markov.run_sql_file(c, credit.SQL_FILE)
    yield c
    c.rollback()


@pytest.fixture(scope="module")
def cfg():
    from recon_engine.datagen.calibration import model_config
    return model_config()


def test_point_in_time_ties_to_reconciled_balances(conn, cfg):
    t = credit.tie_out(conn, markov.to_date(cfg["as_of_date"]))
    assert t["lines_with_difference"] == 0 and t["abs_difference_total"] == 0.0, t


def test_later_data_cannot_change_an_earlier_certificate(conn, cfg):
    prm = credit._params(cfg)
    d = "2025-06-30"
    before = credit.certificate(conn, d, cfg, prm, seed=1)
    conn.execute("savepoint s")
    conn.execute("update core.fact_remit_line set paid_amount = 0, denied_amount = line_charge where payment_date > %s", (d,))
    conn.execute("update core.fact_claim_event set status = 'WRITTEN_OFF' where event_date > %s", (d,))
    conn.execute("update core.fact_claim_line set line_charge = line_charge * 3 where submission_date > %s", (d,))
    after = credit.certificate(conn, d, cfg, prm, seed=1)
    conn.execute("rollback to savepoint s")
    pd.testing.assert_frame_equal(before["table"], after["table"])


def test_certificate_is_deterministic(conn, cfg):
    prm = credit._params(cfg)
    a = credit.certificate(conn, "2025-03-31", cfg, prm, seed=2)
    b = credit.certificate(conn, "2025-03-31", cfg, prm, seed=2)
    pd.testing.assert_frame_equal(a["table"], b["table"])
    assert a["lgd"] == b["lgd"]
