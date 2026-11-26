"""Unit tests for the economics module's formulas and the claim-clustered bootstrap."""
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from recon_engine.models import econ  # noqa: E402


# ---------------------------------------------------------------- break-even appeal rule
def test_break_even_threshold():
    assert econ.appeal_threshold(45.0, 0.5) == pytest.approx(90.0)
    assert econ.appeal_threshold(45.0, 0.9) == pytest.approx(50.0)
    with pytest.raises(ValueError):
        econ.appeal_threshold(45.0, 0.0)


def test_appeal_only_above_threshold_has_positive_value():
    cost, w = 45.0, 0.45
    thr = econ.appeal_threshold(cost, w)                       # 100
    assert econ.expected_net_recovery(thr, w, cost) == pytest.approx(0.0)
    assert econ.expected_net_recovery(thr + 1, w, cost) > 0
    assert econ.expected_net_recovery(thr - 1, w, cost) < 0


# ---------------------------------------------------------------- float
def test_float_value_simple_interest():
    # $1,000,000 held 10 days at 3.65% a year = $1,000
    assert econ.float_value(1_000_000, 0.0365, 10) == pytest.approx(1000.0)
    assert econ.float_value(500.0, 0.05, 0) == 0.0


# ---------------------------------------------------------------- payer incentive
def test_payer_gain_and_deterrence_penalty():
    s, q, w = 200.0, 1.0, 0.5
    # no penalty: the payer keeps half the time, so underpaying pays
    assert econ.payer_expected_gain(s, q, w) == pytest.approx(100.0)
    pi = econ.deterrence_penalty(q, w)
    assert pi == pytest.approx(1.0)                            # repay double to break even
    assert econ.payer_expected_gain(s, q, w, penalty=pi) == pytest.approx(0.0)
    # never appealed: no penalty can deter
    assert econ.deterrence_penalty(0.0, w) == float("inf")
    # the payer's own appeal-handling cost lowers its gain
    assert econ.payer_expected_gain(s, q, w, payer_admin_cost=30) == pytest.approx(70.0)


def test_repeated_game_minimum_loss():
    # G <= delta/(1-delta) * L  <=>  L >= G (1-delta)/delta
    assert econ.repeated_game_min_loss(900.0, 0.9) == pytest.approx(100.0)
    G, d = 1234.0, 0.8
    L = econ.repeated_game_min_loss(G, d)
    assert G == pytest.approx(d / (1 - d) * L)
    with pytest.raises(ValueError):
        econ.repeated_game_min_loss(1.0, 1.0)


# ---------------------------------------------------------------- bootstrap
def _clustered_sample(n_claims=800, seed=1):
    """Claims with 1-4 lines; whether a claim is underpaid is decided once for all its lines."""
    rng = np.random.default_rng(seed)
    n_lines = rng.integers(1, 5, size=n_claims)
    under = rng.random(n_claims) < 0.10
    expected = n_lines * rng.uniform(50, 500, size=n_claims)
    shortfall = np.where(under, 0.08 * expected, 0.0)
    X = np.column_stack([np.ones(n_claims), under, shortfall, expected])
    return X, n_lines, under


def test_bootstrap_is_deterministic_for_a_seed():
    X, _, _ = _clustered_sample()
    a = econ.bootstrap_sums(X, 300, seed=42)
    b = econ.bootstrap_sums(X, 300, seed=42)
    c = econ.bootstrap_sums(X, 300, seed=43)
    assert np.array_equal(a, b)
    assert not np.array_equal(a, c)
    # chunking must not change the draws' distribution shape or determinism
    d = econ.bootstrap_sums(X, 300, seed=42, chunk=50)
    assert d.shape == a.shape and np.array_equal(d, econ.bootstrap_sums(X, 300, seed=42, chunk=50))


def test_bootstrap_interval_covers_point_estimate_and_population_rate():
    X, _, _ = _clustered_sample(n_claims=3000, seed=7)
    draws = econ.bootstrap_sums(X, 1000, seed=11)
    share = draws[:, 1] / draws[:, 0]
    lo, hi = econ.percentile_ci(share)
    point = X[:, 1].sum() / X[:, 0].sum()
    assert lo < point < hi
    assert lo < 0.10 < hi
    # the ratio-of-sums shortfall rate is exactly 8% in every replicate where any claim is underpaid
    rate = draws[:, 2] / draws[:, 3]
    assert np.all(rate < 0.08 + 1e-12)


def test_clustered_interval_wider_than_line_level_interval():
    """Treating the lines of one claim as independent understates uncertainty."""
    X, n_lines, under = _clustered_sample(n_claims=1500, seed=3)
    claim_draws = econ.bootstrap_sums(X[:, :2] * n_lines[:, None], 1000, seed=5)   # lines, underpaid lines
    line_rows = np.repeat(np.column_stack([np.ones(len(under)), under.astype(float)]), n_lines, axis=0)
    line_draws = econ.bootstrap_sums(line_rows, 1000, seed=5)
    w_claim = np.diff(econ.percentile_ci(claim_draws[:, 1] / claim_draws[:, 0]))[0]
    w_line = np.diff(econ.percentile_ci(line_draws[:, 1] / line_draws[:, 0]))[0]
    assert w_claim > 1.2 * w_line


def test_wilson_interval():
    lo, hi = econ.wilson_ci(50, 100)
    assert lo == pytest.approx(0.4038, abs=1e-3) and hi == pytest.approx(0.5962, abs=1e-3)
