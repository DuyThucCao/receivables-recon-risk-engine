"""Q3 cash forecast (models/cashflow.py): Kaplan-Meier and parametric fits on hand examples, the
weekly timing arithmetic, shortfall metrics, determinism, no look-ahead, and recovery of the
planted first-pass payment lag.

Database tests skip when no built database is reachable and roll back everything they change.
"""
import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recon_engine.models import cashflow, markov  # noqa: E402


def test_kaplan_meier_by_hand():
    t = [1, 2, 2, 3, 4]
    d = [1, 1, 0, 1, 0]
    km = cashflow.weighted_km(t, d)
    # S(1) = 4/5; at t=2 risk set 4 (the censored 2 still at risk), 1 event: S = 0.8 * 3/4; t=3: 2 at risk
    assert km["S"].tolist() == pytest.approx([0.8, 0.6, 0.3])
    assert km["n"].tolist() == pytest.approx([5, 4, 2])


def test_integer_weights_equal_duplicated_rows_and_match_lifelines():
    rng = np.random.default_rng(0)
    t = rng.integers(1, 60, 300).astype(float)
    d = (rng.random(300) < 0.7).astype(float)
    w = rng.integers(1, 4, 300).astype(float)
    a = cashflow.weighted_km(t, d, w)
    b = cashflow.weighted_km(np.repeat(t, w.astype(int)), np.repeat(d, w.astype(int)))
    assert np.allclose(a["S"], b["S"])
    lifelines = pytest.importorskip("lifelines")
    kmf = lifelines.KaplanMeierFitter().fit(t, d, weights=w)
    ref = kmf.survival_function_at_times(a["t"].to_numpy()).to_numpy()
    assert np.allclose(a["S"].to_numpy(), ref, atol=1e-10)


def test_km_on_grid_steps():
    km = cashflow.weighted_km([1, 2, 2, 3, 4], [1, 1, 0, 1, 0])
    S, n = cashflow.km_on_grid(km, 5)
    assert S.tolist() == pytest.approx([1.0, 0.8, 0.6, 0.3, 0.3, 0.3])


def test_parametric_fit_recovers_lognormal_with_censoring():
    rng = np.random.default_rng(1)
    T = rng.lognormal(mean=np.log(30), sigma=0.5, size=4000)
    C = rng.uniform(5, 120, size=4000)
    t, d = np.minimum(T, C), (T <= C).astype(float)
    fits = {f: cashflow.fit_parametric(t, d, np.ones_like(t), f) for f in cashflow.FAMILIES}
    ln = fits["lognormal"]["params"]
    assert ln["median_days"] == pytest.approx(30, rel=0.03)
    assert ln["sigma"] == pytest.approx(0.5, rel=0.05)
    assert min(fits, key=lambda k: fits[k]["aic"]) == "lognormal"      # chosen by fit quality


def test_weekly_probabilities_timing():
    cdf = np.zeros(200)
    cdf[10:] = 1.0                       # every claim is paid exactly 10 days after submission
    dep = np.array([1.0])                # same-day deposit
    w = cashflow.weekly_probabilities(cdf, 0, dep, 91)
    assert w[1] == pytest.approx(1.0) and w.sum() == pytest.approx(1.0)   # offset 10 -> week 2
    w5 = cashflow.weekly_probabilities(cdf, 5, dep, 91)
    assert w5[0] == pytest.approx(1.0)   # already 5 days old -> paid on day 5 after the forecast date
    dep2 = np.array([0.0, 0.0, 0.0, 0.0, 1.0])  # 4-day deposit lag pushes day 5 into week 2
    assert cashflow.weekly_probabilities(cdf, 5, dep2, 91)[1] == pytest.approx(1.0)


def test_shortfall_metrics_by_hand():
    sims = np.full((4, 3), 100.0)
    sims[0] = [0, 0, 0]
    m = cashflow.shortfall_metrics(sims, opening=150.0, need=100.0, min_balance=0.0)
    # sim 0: positions 50, -50, -150 -> shortfall 150; others: 150 each week -> 0
    assert m["p_shortfall"] == pytest.approx(0.25)
    assert m["expected_shortfall_mean"] == pytest.approx(37.5)
    assert m["shortfall_cvar95"] == pytest.approx(150.0)
    assert m["receipts_13w_mean"] == pytest.approx(225.0)


# ------------------------------------------------------------------ database tests
@pytest.fixture(scope="module")
def conn():
    from recon_engine import db
    try:
        c = db.connect()
        n = c.execute("select count(*) from recon.remit_trace").fetchone()[0]
    except Exception as e:  # pragma: no cover
        pytest.skip(f"no built database reachable: {e}")
    if not n:
        pytest.skip("database has no reconciled data")
    markov.ensure_sql(c)
    markov.run_sql_file(c, cashflow.SQL_FILE)
    yield c
    c.rollback()


@pytest.fixture(scope="module")
def cfg():
    from recon_engine.datagen.calibration import model_config
    return model_config()


def test_deterministic(conn, cfg):
    a = cashflow.forecast(conn, "2025-03-03", cfg, n_sims=200, seed=5)
    b = cashflow.forecast(conn, "2025-03-03", cfg, n_sims=200, seed=5)
    assert np.array_equal(a["sims"], b["sims"])


def test_later_data_cannot_change_an_earlier_forecast(conn, cfg):
    f = "2025-03-03"
    before = cashflow.forecast(conn, f, cfg, n_sims=200, seed=11)
    conn.execute("savepoint s")
    conn.execute("update core.fact_remit_line set paid_amount = paid_amount * 3 where payment_date > %s", (f,))
    conn.execute("update core.fact_claim_event set status = 'WRITTEN_OFF' where event_date > %s", (f,))
    conn.execute("delete from core.fact_claim_event where event_date > %s::date + 45", (f,))
    conn.execute("update core.fact_bank_txn set amount = amount * 2, running_balance = 0 where posting_date > %s", (f,))
    conn.execute("update core.fact_claim_line set line_charge = line_charge * 5 where submission_date > %s", (f,))
    after = cashflow.forecast(conn, f, cfg, n_sims=200, seed=11)
    conn.execute("rollback to savepoint s")
    assert np.array_equal(before["sims"], after["sims"])
    assert before["n_open_claims"] == after["n_open_claims"]


def test_first_pass_lag_recovers_planted_quartiles(conn, cfg):
    planted = json.loads((ROOT / "data" / "truth" / "planted_parameters.json").read_text())
    ev = cashflow.evaluate_against_planted(conn, planted, cutoffs=(cfg["as_of_date"],))
    assert ev[str(cfg["as_of_date"])]["max_abs_error_quartiles_days"] <= 3.0, ev
