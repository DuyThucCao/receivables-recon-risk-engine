"""Q4 reserve: hand-checked chain-ladder, a published benchmark, bootstrap determinism, and
no look-ahead in the SQL triangle (database tests skip when no built database is reachable)."""
import math
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from recon_engine.models import reserve as R  # noqa: E402

NAN = np.nan


# ------------------------------------------------------------------ pure functions
def test_chain_ladder_3x3_by_hand():
    # incremental triangle
    inc = np.array([[100.0, 50.0, 25.0],
                    [110.0, 55.0, NAN],
                    [120.0, NAN, NAN]])
    cum = R.to_cumulative(inc)
    assert np.allclose(cum[0], [100, 150, 175])
    assert np.allclose(cum[1, :2], [110, 165]) and np.isnan(cum[1, 2])
    cl = R.chain_ladder(cum)
    # f0 = (150 + 165) / (100 + 110) = 1.5 ; f1 = 175 / 150 = 7/6
    assert cl["f"] == pytest.approx([1.5, 7 / 6])
    assert cl["cdf"] == pytest.approx([1.75, 7 / 6, 1.0])
    # ultimates: 175 ; 165 * 7/6 = 192.5 ; 120 * 1.75 = 210
    assert cl["ultimate"] == pytest.approx([175.0, 192.5, 210.0])
    assert cl["unpaid"] == pytest.approx([0.0, 27.5, 90.0])
    assert cl["unpaid"].sum() == pytest.approx(117.5)


def test_bornhuetter_ferguson_cape_cod_by_hand():
    inc = np.array([[100.0, 50.0, 25.0], [110.0, 55.0, NAN], [120.0, NAN, NAN]])
    cl = R.chain_ladder(R.to_cumulative(inc))
    expo = np.array([250.0, 250.0, 250.0])
    # used-up exposure = 250/1 + 250/(7/6) + 250/1.75 ; paid = 175 + 165 + 120
    used = 250 + 250 * 6 / 7 + 250 / 1.75
    elr = 460 / used
    bf = R.bornhuetter_ferguson(cl, expo)
    assert bf["elr"] == pytest.approx(elr)
    assert bf["unpaid"] == pytest.approx([0.0, elr * 250 * (1 - 6 / 7), elr * 250 * (1 - 1 / 1.75)])


def test_fitted_incrementals_reproduce_latest_diagonal():
    inc = np.array([[100.0, 50.0, 25.0], [110.0, 55.0, NAN], [120.0, NAN, NAN]])
    cum = R.to_cumulative(inc)
    cl = R.chain_ladder(cum)
    m = R.fitted_incrementals(cum, cl["f"])
    # row sums of fitted incrementals equal the latest cumulative (ODP fit property)
    assert np.nansum(m, axis=1) == pytest.approx([175.0, 165.0, 120.0])
    # row 1: back-cast 165 / 1.5 = 110 at lag 0, so the fit is exact there
    assert m[1, 0] == pytest.approx(110.0)


# Taylor & Ashe (1983) cumulative paid triangle, the benchmark used by Mack (1993).
GENINS = [
    [357848, 1124788, 1735330, 2218270, 2745596, 3319994, 3466336, 3606286, 3833515, 3901463],
    [352118, 1236139, 2170033, 3353322, 3799067, 4120063, 4647867, 4914039, 5339085],
    [290507, 1292306, 2218525, 3235179, 3985995, 4132918, 4628910, 4909315],
    [310608, 1418858, 2195047, 3757447, 4029929, 4381982, 4588268],
    [443160, 1136350, 2128333, 2897821, 3402672, 3873311],
    [396132, 1333217, 2180715, 2985752, 3691712],
    [440832, 1288463, 2419861, 3483130],
    [359480, 1421128, 2864498],
    [376686, 1363294],
    [344014],
]


def _genins():
    cum = np.full((10, 10), NAN)
    for i, row in enumerate(GENINS):
        cum[i, :len(row)] = row
    return cum


def test_chain_ladder_and_mack_match_published_benchmark():
    cum = _genins()
    cl = R.chain_ladder(cum)
    # published chain-ladder reserve for this triangle: 18,680,856 (Mack 1993)
    assert cl["unpaid"].sum() == pytest.approx(18_680_856, abs=2)
    mk = R.mack(cum, cl)
    # published Mack standard error of the total reserve: 2,447,095 (13% of the reserve)
    assert mk["se_total"] == pytest.approx(2_447_095, rel=2e-3)


def test_odp_bootstrap_is_deterministic_and_centred():
    cum = _genins()
    inc = np.diff(np.concatenate([np.zeros((10, 1)), cum], axis=1), axis=1)
    a = R.odp_bootstrap(inc, n_boot=400, seed=7)
    b = R.odp_bootstrap(inc, n_boot=400, seed=7)
    assert np.array_equal(a["totals"], b["totals"])
    cl_total = R.chain_ladder(cum)["unpaid"].sum()
    assert abs(a["totals"].mean() / cl_total - 1) < 0.06
    # the bootstrap s.e. should be of the same order as Mack's (they estimate the same quantity)
    se = R.mack(cum)["se_total"]
    assert 0.6 < a["totals"].std() / se < 1.6


def test_calendar_projection_sums_next_diagonals():
    inc = np.array([[100.0, 50.0, 25.0], [110.0, 55.0, NAN], [120.0, NAN, NAN]])
    cl = R.chain_ladder(R.to_cumulative(inc))
    # next calendar month: row1 lag2 (27.5) + row2 lag1 (120*0.5 = 60); month after: row2 lag2 (180*(1/6) = 30)
    assert R.calendar_projection(cl["full"], cl["latest_lag"], 1) == pytest.approx(87.5)
    assert R.calendar_projection(cl["full"], cl["latest_lag"], 2) == pytest.approx(117.5)


def test_lag_bands_cover_all_lags():
    assert [R.lag_band(k) for k in (0, 1, 2, 3, 4, 5, 6, 30)] == ["0", "1", "2", "3", "4-5", "4-5", "6+", "6+"]


def test_module_never_mentions_ground_truth_folder():
    assert "truth" not in (ROOT / "src" / "recon_engine" / "models" / "reserve.py").read_text().lower()


# ------------------------------------------------------------------ database tests
@pytest.fixture(scope="module")
def conn():
    from recon_engine import db
    try:
        c = db.connect()
        n = c.execute("select count(*) from recon.claim_remit_match").fetchone()[0]
    except Exception as e:  # pragma: no cover - depends on the environment
        pytest.skip(f"no built database reachable: {e}")
    if not n:
        pytest.skip("database has no reconciled data")
    c.execute((ROOT / "sql" / "models" / "50_reserve_triangle.sql").read_text())
    yield c
    c.rollback()
    c.close()


def _tri(c, d):
    return c.execute("select accident_month, dev_lag, incremental_paid, n_claims_first_paid, n_claims_reported "
                     "from mart.dev_triangle_at(%s::date) order by 1, 2", (d,)).fetchall()


def test_triangle_ties_to_reconciliation_core(conn):
    asof = conn.execute("select recon.param('as_of_date')::date").fetchone()[0]
    tri = conn.execute("select sum(incremental_paid) from mart.dev_triangle_at(%s)", (asof,)).fetchone()[0]
    core = conn.execute("select sum(paid) from recon.claim_line_balance b join core.fact_claim_line c using (claim_line_id) "
                        "where c.submission_date <= %s", (asof,)).fetchone()[0]
    assert tri == core


def test_future_postings_cannot_change_an_earlier_triangle(conn):
    retro = "2025-03-31"
    before = _tri(conn, retro)
    conn.execute("savepoint s")
    # tamper with everything after the retro date: inflate payments, add claims' postings later
    conn.execute("update core.fact_remit_line set paid_amount = paid_amount + 1000 where payment_date > %s", (retro,))
    conn.execute("update core.fact_claim_line set submission_date = submission_date where submission_date > %s", (retro,))
    after = _tri(conn, retro)
    later = conn.execute("select sum(incremental_paid) from mart.dev_triangle_at('2026-03-31')").fetchone()[0]
    conn.execute("rollback to savepoint s")
    assert before == after
    assert later != conn.execute("select sum(incremental_paid) from mart.dev_triangle_at('2026-03-31')").fetchone()[0]


def test_estimate_is_deterministic(conn):
    a = R.estimate(conn, "2025-06-30", n_boot=200, seed=11)
    b = R.estimate(conn, "2025-06-30", n_boot=200, seed=11)
    assert np.array_equal(a["boot"]["totals"], b["boot"]["totals"])
    assert a["cl"]["unpaid"].sum() == b["cl"]["unpaid"].sum()
