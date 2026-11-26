"""Q2 prioritize: exact knapsack checked by enumeration, metrics checked by hand, the appeal-window
rule, the daily replay, and no look-ahead in the training data (database tests skip when no built
database is reachable)."""
import itertools
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from recon_engine.models import prioritize as P  # noqa: E402


def _brute(values, weights, cap):
    best, best_set = 0.0, ()
    for k in range(len(values) + 1):
        for s in itertools.combinations(range(len(values)), k):
            if sum(weights[i] for i in s) <= cap:
                v = sum(values[i] for i in s)
                if v > best + 1e-9:
                    best, best_set = v, s
    return best, best_set


# ------------------------------------------------------------------ knapsack
def test_knapsack_beats_greedy_on_textbook_case():
    # value density 6, 5, 4: greedy by density takes items 0 and 1 (value 160) and then 2 does not fit;
    # the optimum is items 1 and 2 (value 220).
    v, w = [60, 100, 120], [10, 20, 30]
    assert P.knapsack_01(v, w, 50) == [1, 2]
    assert _brute(v, w, 50)[0] == 220


@pytest.mark.parametrize("seed", range(25))
def test_knapsack_matches_enumeration(seed):
    rng = np.random.default_rng(seed)
    n = int(rng.integers(1, 11))
    values = rng.normal(50, 60, n).round(2).tolist()          # some negative values
    weights = rng.integers(5, 60, n).tolist()
    cap = int(rng.integers(0, 200))
    chosen = P.knapsack_01(values, weights, cap)
    best, _ = _brute(values, weights, cap)
    assert sum(weights[i] for i in chosen) <= cap
    assert sum(values[i] for i in chosen) == pytest.approx(best)
    assert all(values[i] > 0 for i in chosen)


def test_knapsack_takes_all_positive_items_when_they_fit():
    assert P.knapsack_01([5, -1, 3, 0], [10, 10, 10, 10], 100) == [0, 2]


def test_equal_weights_reduce_to_top_k_by_value():
    rng = np.random.default_rng(3)
    v = rng.uniform(1, 100, 30)
    chosen = P.knapsack_01(v, [35] * 30, 35 * 7)
    assert sorted(chosen) == sorted(np.argsort(-v)[:7].tolist())


def test_first_fit_skips_items_that_do_not_fit():
    assert P.first_fit([0, 1, 2, 3], [30, 50, 20, 10], 60) == [0, 2, 3]


# ------------------------------------------------------------------ metrics
def test_classification_metrics_by_hand():
    m = P.classification_metrics(np.array([1, 0]), np.array([0.8, 0.4]))
    assert m["brier"] == pytest.approx((0.2 ** 2 + 0.4 ** 2) / 2)
    assert m["log_loss"] == pytest.approx(-(math.log(0.8) + math.log(0.6)) / 2)
    assert m["auc"] == 1.0


def test_reliability_table_bins_and_rates():
    y = np.array([0, 0, 1, 1, 0, 1, 1, 1])
    p = np.array([0.1, 0.2, 0.3, 0.4, 0.6, 0.7, 0.8, 0.9])
    rel = P.reliability_table(y, p, n_bins=2)
    assert [r["n"] for r in rel] == [4, 4]
    assert rel[0]["observed_rate"] == pytest.approx(0.5) and rel[1]["observed_rate"] == pytest.approx(0.75)


# ------------------------------------------------------------------ value, window and replay
class _Const:
    """Stand-in for OverturnModel with a fixed probability."""
    def __init__(self, p):
        self.p = p

    def predict(self, df):
        return np.full(len(df), self.p)


def _pool():
    return pd.DataFrame({
        "claim_id": ["A", "B", "C", "D"],
        "denial_seq": [1, 1, 1, 1],
        "payer_id": ["P01"] * 4,
        "reason_category": ["ADMINISTRATIVE"] * 4,
        "denial_date": pd.to_datetime(["2024-12-01", "2025-03-20", "2025-03-31", "2025-04-02"]),
        "contract_allowed": [1000.0, 200.0, 5000.0, 9000.0],
        "billed": [2000.0, 400.0, 9000.0, 15000.0],
    })


def _h(uniform=True):
    cfg = {"financial_assumptions": {"appeal_handling_minutes": {"value": 30}, "appeal_handling_cost_usd": {"value": 30.0},
                                     "denial_staff_hours_per_day": {"value": 1}}}
    return P.handling(cfg, uniform=uniform)


def test_value_is_zero_past_the_appeal_window():
    sc = P.score(_pool(), _Const(0.5), {"P01": 1.0}, pd.Timestamp("2025-04-02"), _h(), window_days=90)
    a = sc.set_index("claim_id")
    assert a.loc["A", "days_left_to_appeal"] < 0 and a.loc["A", "expected_value"] == 0.0
    # B: 0.5 * 200 - 30 minutes * $1 = 70
    assert a.loc["B", "expected_value"] == pytest.approx(70.0)
    assert P.choose(sc, "MODEL_KNAPSACK", 10_000) == [1, 2, 3]
    assert 0 not in P.choose(sc, "OLDEST_FIRST", 10_000)


def test_replay_never_works_a_claim_before_its_denial_or_after_its_window():
    sc = P.score(_pool(), _Const(0.5), {"P01": 1.0}, pd.Timestamp("2025-04-30"), _h(), window_days=90)
    sc["expected_value_raw"] = sc["p_overturn"] * sc["expected_recoverable"] - sc["handling_cost"]
    for pol in P.POLICIES:
        tot, w = P.replay(sc, pol, 0.5, pd.Timestamp("2025-03-17"), pd.Timestamp("2025-04-30"), 90)
        assert "A" not in set(w["claim_id"])                         # window closed before the replay began
        assert (w["work_date"] >= w["denial_date"]).all()
        assert ((w["work_date"] - w["denial_date"]).dt.days <= 90).all()
        assert tot["minutes_used"] <= 30 * len(pd.bdate_range("2025-03-17", "2025-04-30"))


def test_replay_one_claim_per_day_budget_orders_by_policy():
    sc = P.score(_pool(), _Const(0.5), {"P01": 1.0}, pd.Timestamp("2025-04-30"), _h(), window_days=90)
    sc["expected_value_raw"] = sc["p_overturn"] * sc["expected_recoverable"] - sc["handling_cost"]
    # budget of 30 minutes = one claim per day; on 2025-04-02 both C and D are open (B worked earlier)
    _, w = P.replay(sc, "MODEL_KNAPSACK", 0.5, pd.Timestamp("2025-04-01"), pd.Timestamp("2025-04-03"), 90)
    first = w.sort_values("work_date").iloc[0]
    assert first["claim_id"] == "C"                                    # only C and B are open on 04-01; C is worth more
    _, w2 = P.replay(sc, "OLDEST_FIRST", 0.5, pd.Timestamp("2025-04-01"), pd.Timestamp("2025-04-03"), 90)
    assert w2.sort_values("work_date").iloc[0]["claim_id"] == "B"


def test_module_never_mentions_ground_truth_folder():
    assert "truth" not in (ROOT / "src" / "recon_engine" / "models" / "prioritize.py").read_text().lower()


# ------------------------------------------------------------------ database tests
@pytest.fixture(scope="module")
def conn():
    from recon_engine import db
    try:
        c = db.connect()
        n = c.execute("select count(*) from core.fact_claim_event where status = 'APPEALED'").fetchone()[0]
    except Exception as e:  # pragma: no cover - depends on the environment
        pytest.skip(f"no built database reachable: {e}")
    if n < 200:
        pytest.skip("database has too few appeals for a model fit")
    c.execute((ROOT / "sql" / "models" / "55_work_queue.sql").read_text())
    yield c
    c.rollback()
    c.close()


def test_training_data_and_fit_cannot_see_the_future(conn):
    cut = P.VALIDATION_CUTOFF
    before = P.denial_episodes(conn, cut)
    m1 = P.OverturnModel(before[before["appeal_outcome"].notna()])
    conn.execute("savepoint s")
    # rewrite history after the cutoff: drop every later event and every later posting
    conn.execute("delete from core.fact_claim_event where event_date > %s", (cut,))
    conn.execute("update core.fact_remit_line set paid_amount = 0 where payment_date > %s", (cut,))
    after = P.denial_episodes(conn, cut)
    conn.execute("rollback to savepoint s")
    pd.testing.assert_frame_equal(before, after)
    m2 = P.OverturnModel(after[after["appeal_outcome"].notna()])
    assert np.allclose(m1.result.params.values, m2.result.params.values)
    assert before["denial_date"].max() <= pd.Timestamp(cut)
    assert before["outcome_date"].dropna().max() <= pd.Timestamp(cut)


def test_fit_is_deterministic(conn):
    ep = P.denial_episodes(conn, P.VALIDATION_CUTOFF)
    tr = ep[ep["appeal_outcome"].notna()]
    a, b = P.OverturnModel(tr), P.OverturnModel(tr)
    assert np.array_equal(a.result.params.values, b.result.params.values)
