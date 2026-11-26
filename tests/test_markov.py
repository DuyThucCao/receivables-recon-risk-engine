"""Absorbing Markov chain (models/markov.py): hand-computable chains, estimation mechanics,
the time-in-state correction, determinism, no look-ahead and recovery of the planted matrices.

Database tests skip when no built database is reachable. They run inside one transaction that is
rolled back, so the database is left as it was.
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recon_engine.models import markov  # noqa: E402
from recon_engine.models.markov import IDX, STATES, TRANSIENT  # noqa: E402


# ------------------------------------------------------------------ hand-computable chains
def test_three_state_chain_by_hand():
    # one transient state T with P(T->T)=0.5, P(T->PAID)=0.3, P(T->WO)=0.2
    P = np.array([[0.5, 0.3, 0.2], [0, 1, 0], [0, 0, 1]])
    q = markov.absorbing_quantities(P, n_transient=1)
    assert q["N"][0, 0] == pytest.approx(2.0)                 # 1 / (1 - 0.5)
    assert q["B"][0] == pytest.approx([0.6, 0.4])             # 0.3 / 0.5, 0.2 / 0.5
    assert q["t"][0] == pytest.approx(2.0)                    # steps ~ Geometric(0.5): mean 2
    assert q["sd_t"][0] == pytest.approx(np.sqrt(2.0))        # variance (1-p)/p^2 = 2


def test_two_transient_states_by_hand():
    # A -> B 0.5, A -> PAID 0.5 ; B -> A 0.2, B -> WO 0.8
    P = np.array([[0, 0.5, 0.5, 0], [0.2, 0, 0, 0.8], [0, 0, 1, 0], [0, 0, 0, 1]])
    q = markov.absorbing_quantities(P, n_transient=2)
    N_hand = np.array([[1, 0.5], [0.2, 1]]) / 0.9             # inverse of [[1,-.5],[-.2,1]], det 0.9
    assert np.allclose(q["N"], N_hand)
    assert np.allclose(q["B"], N_hand @ np.array([[0.5, 0], [0, 0.8]]))
    assert np.allclose(q["B"].sum(axis=1), 1.0)               # absorption is certain
    assert q["B"][0, 0] == pytest.approx(0.5 / 0.9)


def test_mle_and_pooled_fallback():
    C = np.zeros((5, 7))
    C[IDX["IN_REVIEW"], IDX["PAID"]] = 80
    C[IDX["IN_REVIEW"], IDX["DENIED"]] = 20
    C[IDX["DENIED"], IDX["WRITTEN_OFF"]] = 3                  # sparse row
    pooled = np.zeros((5, 7))
    pooled[IDX["DENIED"], IDX["IN_REVIEW"]] = 60
    pooled[IDX["DENIED"], IDX["WRITTEN_OFF"]] = 40
    P, flags = markov.mle_matrix(C, pooled, min_row_count=50)
    assert P[IDX["IN_REVIEW"], IDX["PAID"]] == pytest.approx(0.8)
    assert flags[IDX["DENIED"]] and not flags[IDX["IN_REVIEW"]]
    assert P[IDX["DENIED"], IDX["IN_REVIEW"]] == pytest.approx(0.6)
    assert np.allclose(P.sum(axis=1), 1.0)


def _events(paths: dict[str, list[tuple[str, str]]], payer="P01") -> pd.DataFrame:
    rows = []
    for cid, evs in paths.items():
        for k, (s, d) in enumerate(evs, start=1):
            rows.append({"claim_id": cid, "payer_id": payer, "seq": k, "status": s, "event_date": pd.Timestamp(d),
                         "reason_category": None})
    return pd.DataFrame(rows)


def test_extract_transitions_drops_repeats_and_post_absorbing_and_immature():
    ev = _events({
        "c1": [("SUBMITTED", "2025-01-01"), ("IN_REVIEW", "2025-01-02"), ("IN_REVIEW", "2025-01-03"), ("PAID", "2025-01-20"),
               ("DENIED", "2025-02-01")],
        "c2": [("SUBMITTED", "2025-06-01"), ("IN_REVIEW", "2025-06-02")],
    })
    pairs, notes = markov.extract_transitions(ev, "2025-06-30", maturity_days=0)
    assert notes["dropped_self_repeat"] == 1 and notes["dropped_after_absorbing"] == 1
    assert set(zip(pairs["from_state"], pairs["to_state"])) == {("SUBMITTED", "IN_REVIEW"), ("IN_REVIEW", "PAID")}
    pairs90, notes90 = markov.extract_transitions(ev, "2025-06-30", maturity_days=90)
    assert "c2" not in set(pairs90["claim_id"])               # visit began less than 90 days before cutoff
    assert notes90["dropped_immature_exits"] == 1


def _simulate_events(P: np.ndarray, n: int, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n):
        s, d, k = "SUBMITTED", pd.Timestamp("2024-01-01") + pd.Timedelta(days=int(rng.integers(0, 100))), 1
        rows.append({"claim_id": f"c{i}", "payer_id": "P01", "seq": k, "status": s, "event_date": d, "reason_category": None})
        while s not in ("PAID", "WRITTEN_OFF"):
            s = STATES[rng.choice(7, p=P[IDX[s]])]
            d, k = d + pd.Timedelta(days=int(rng.integers(1, 30))), k + 1
            rows.append({"claim_id": f"c{i}", "payer_id": "P01", "seq": k, "status": s, "event_date": d, "reason_category": None})
    return pd.DataFrame(rows)


TRUE_P = np.zeros((7, 7))
TRUE_P[0, 1] = 1.0
TRUE_P[1, [2, 3, 5]] = [0.05, 0.15, 0.80]
TRUE_P[2, [1, 6]] = [0.95, 0.05]
TRUE_P[3, [1, 4, 6]] = [0.35, 0.25, 0.40]
TRUE_P[4, [3, 5]] = [0.5, 0.5]
TRUE_P[5, 5] = TRUE_P[6, 6] = 1.0


def test_recovers_known_matrix_and_is_deterministic():
    ev = _simulate_events(TRUE_P, 4000, seed=7)
    a = markov.fit_from_events(ev, "2026-01-01", maturity_days=0, n_boot=100, seed=3)
    b = markov.fit_from_events(ev, "2026-01-01", maturity_days=0, n_boot=100, seed=3)
    est = a.P["P01"][:5]
    n = a.counts["P01"].sum(axis=1)
    se = np.sqrt(np.clip(TRUE_P[:5] * (1 - TRUE_P[:5]), 1e-12, None) / n[:, None])
    assert np.max(np.abs(est - TRUE_P[:5]) / se) < 4.5
    assert a.boot["P01"] == b.boot["P01"]                     # same seed, same bootstrap
    b_true = markov.absorbing_quantities(TRUE_P)["B"][0, 0]
    lo, hi = a.boot["P01"]["p_paid_from_submitted_lo"], a.boot["P01"]["p_paid_from_submitted_hi"]
    assert lo - 0.01 <= b_true <= hi + 0.01


def test_time_in_state_correction():
    # DENIED -> IN_REVIEW after ~5 days, DENIED -> WRITTEN_OFF after ~100 days. A claim already 50 days
    # in DENIED can only be heading for write-off, so its P(PAID) must be 0; the memoryless chain
    # gives every DENIED claim the same positive probability.
    paths = {}
    for i in range(200):
        if i % 2:
            paths[f"r{i}"] = [("SUBMITTED", "2024-01-01"), ("IN_REVIEW", "2024-01-02"), ("DENIED", "2024-02-01"),
                              ("IN_REVIEW", "2024-02-06"), ("PAID", "2024-03-01")]
        else:
            paths[f"w{i}"] = [("SUBMITTED", "2024-01-01"), ("IN_REVIEW", "2024-01-02"), ("DENIED", "2024-02-01"),
                              ("WRITTEN_OFF", "2024-05-11")]
    ch = markov.fit_from_events(_events(paths), "2025-06-30", maturity_days=0, min_row_count=10)
    memoryless = ch.p_paid("DENIED", "P01")
    assert memoryless > 0.2
    assert ch.p_paid_open(["DENIED"], ["P01"], [50])[0] == pytest.approx(0.0)
    assert ch.p_paid_open(["DENIED"], ["P01"], [0])[0] == pytest.approx(memoryless)


def test_dollar_weighting_shifts_absorption_towards_large_claims():
    paths = {f"s{i}": [("SUBMITTED", "2024-01-01"), ("IN_REVIEW", "2024-01-02"), ("PAID", "2024-02-01")] for i in range(90)}
    paths.update({f"b{i}": [("SUBMITTED", "2024-01-01"), ("IN_REVIEW", "2024-01-02"), ("DENIED", "2024-02-01"),
                            ("WRITTEN_OFF", "2024-04-01")] for i in range(10)})
    w = pd.Series({**{f"s{i}": 100.0 for i in range(90)}, **{f"b{i}": 9000.0 for i in range(10)}})
    ev = _events(paths)
    c = markov.fit_from_events(ev, "2025-01-01", maturity_days=0, min_row_count=5)
    d = markov.fit_from_events(ev, "2025-01-01", maturity_days=0, min_row_count=5, claim_weights=w)
    assert c.p_paid("SUBMITTED", "P01") == pytest.approx(0.9)
    assert d.p_paid("SUBMITTED", "P01") == pytest.approx(9000 / (9000 + 90000))   # dollar share paid


# ------------------------------------------------------------------ database tests
@pytest.fixture(scope="module")
def conn():
    from recon_engine import db
    try:
        c = db.connect()
        n = c.execute("select count(*) from core.fact_claim_event").fetchone()[0]
    except Exception as e:  # pragma: no cover
        pytest.skip(f"no built database reachable: {e}")
    if not n:
        pytest.skip("database has no claim events")
    markov.ensure_sql(c)
    yield c
    c.rollback()


def test_no_look_ahead(conn):
    cut = "2025-03-31"
    before = markov.fit(conn, cut)
    conn.execute("savepoint s")
    # rewrite the future: every later event becomes a write-off, and a fake later event is added
    conn.execute("update core.fact_claim_event set status = 'WRITTEN_OFF' where event_date > %s", (cut,))
    conn.execute("delete from core.fact_claim_event where event_date > %s::date + 30", (cut,))
    after = markov.fit(conn, cut)
    conn.execute("rollback to savepoint s")
    for g in before.P:
        assert np.array_equal(before.P[g], after.P[g])


def test_recovery_of_planted_matrices(conn):
    planted = json.loads((ROOT / "data" / "truth" / "planted_parameters.json").read_text())
    from recon_engine.datagen.calibration import model_config
    ch = markov.fit(conn, model_config()["as_of_date"], n_boot=200, seed=1)
    ev = markov.evaluate_against_planted(ch, planted)
    assert ev["max_z_all_payers"] <= 4.0, ev
    assert ev["max_abs_error_all_payers"] <= 0.10, ev
    for p, v in ev["per_payer"].items():
        assert v["p_paid_abs_error_vs_mean_over_claims"] <= 0.02, (p, v)
