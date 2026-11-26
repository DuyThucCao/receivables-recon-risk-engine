"""Checks on the economics marts of the built database (skipped when the database or marts are absent)."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from recon_engine import db  # noqa: E402
from recon_engine.models import econ  # noqa: E402

GEN = ROOT / "data" / "truth"


@pytest.fixture(scope="module")
def conn():
    try:
        c = db.connect()
    except Exception as e:  # pragma: no cover
        pytest.skip(f"no database: {e}")
    if c.execute("select to_regclass('mart.underpayment')").fetchone()[0] is None:
        pytest.skip("econ marts not built; run scripts/run_models_econ.py")
    yield c
    c.close()


def frame(conn, sql):
    cur = conn.execute(sql)
    return pd.DataFrame(cur.fetchall(), columns=[d.name for d in cur.description])


def test_gap_reasons_partition_lines_and_dollars(conn):
    r = conn.execute("""select count(*), count(gap_reason),
                               sum(pricing_shortfall) filter (where gap_reason = 'PRICED_BELOW_CONTRACT'),
                               sum(pricing_shortfall) from mart.underpayment_line""").fetchone()
    assert r[0] == r[1] and r[2] == r[3]


def test_payer_summary_equals_line_sums(conn):
    a = frame(conn, "select payer_id, sum(pricing_shortfall)::float8 s from mart.underpayment_line group by 1 order by 1")
    b = frame(conn, "select payer_id, shortfall_usd::float8 s from mart.underpayment order by 1")
    assert np.allclose(a.s, b.s, atol=0.01)


def test_patient_responsibility_never_counted_as_shortfall(conn):
    # a line priced exactly at contract with cost sharing has paid < contract but no shortfall
    n = conn.execute("""select count(*) from mart.underpayment_line
                        where first_allowed = expected_allowed and patient_resp > 0 and pricing_shortfall > 0""").fetchone()[0]
    assert n == 0


def test_float_matches_formula(conn):
    f = frame(conn, "select paid::float8 p, rate::float8 r, days_to_pay d, days_late l, float_total::float8 ft, "
                    "float_late::float8 fl from mart.float_value_line")
    assert np.allclose(econ.float_value(f.p, f.r, f.d), f.ft, atol=0.006)
    assert np.allclose(econ.float_value(f.p, f.r, f.l), f.fl, atol=0.006)
    assert (f.l <= f.d).all()


def test_threshold_rule_in_mart(conn):
    i = frame(conn, "select appeal_cost_usd::float8 c, p_win::float8 w, threshold_usd::float8 t from mart.underpayment_incentive")
    assert np.allclose(i.t, i.c / i.w, atol=0.006)


@pytest.mark.skipif(not (GEN / "claim_line_truth.csv").exists(), reason="generator records absent")
def test_flags_agree_with_generator(conn):
    g = pd.read_csv(GEN / "claim_line_truth.csv", usecols=["claim_id", "line_no", "contract_allowed_cents",
                                                          "payer_allowed_cents_first_price"])
    m = frame(conn, "select claim_id, line_no, pricing_shortfall::float8 s from mart.underpayment_line")
    j = m.merge(g, on=["claim_id", "line_no"])
    flag_gen = (j.contract_allowed_cents - j.payer_allowed_cents_first_price) >= 1
    flag_model = j.s > 0
    recall = (flag_model & flag_gen).sum() / flag_gen.sum()
    precision = (flag_model & flag_gen).sum() / flag_model.sum()
    assert recall > 0.98 and precision > 0.98
