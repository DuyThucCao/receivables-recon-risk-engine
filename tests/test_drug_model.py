"""Checks on the drug marts of the built database (skipped when the database or marts are absent).

Run after building and running the models, for example:
    export RRE_PG_DIR=~/.cache/receivables-engine/pg-econ
    python run_all.py --skip-generate && python scripts/run_models_econ.py && python -m pytest tests/test_drug_model.py
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from recon_engine import db  # noqa: E402

GEN = ROOT / "data" / "truth"


@pytest.fixture(scope="module")
def conn():
    try:
        c = db.connect()
    except Exception as e:  # pragma: no cover - no server available
        pytest.skip(f"no database: {e}")
    if c.execute("select to_regclass('mart.drug_lot_position')").fetchone()[0] is None:
        pytest.skip("drug marts not built; run scripts/run_models_econ.py")
    yield c
    c.close()


def frame(conn, sql):
    cur = conn.execute(sql)
    return pd.DataFrame(cur.fetchall(), columns=[d.name for d in cur.description])


def test_inventory_identity_every_lot_units_and_cents(conn):
    bad = conn.execute("""select count(*) from mart.drug_lot_position
                          where purchased_units <> administered_units + wasted_units + on_hand_units
                             or purchased_value <> administered_cost + wasted_cost + on_hand_value""").fetchone()[0]
    assert bad == 0


def test_lots_tie_to_purchase_invoices(conn):
    r = conn.execute("""select (select sum(extended_amount) from core.fact_drug_purchase
                                where invoice_date <= (select as_of from mart.drug_fifo_param)),
                               (select sum(purchased_value) from mart.drug_lot_position)""").fetchone()
    assert r[0] == r[1]


def test_every_dispensed_unit_is_allocated(conn):
    assert conn.execute("select count(*) from mart.drug_fifo_unallocated").fetchone()[0] == 0
    r = conn.execute("""select (select sum(units) from mart.drug_fifo_event_input),
                               (select sum(qty) from mart.drug_fifo)""").fetchone()
    assert r[0] == r[1]


def test_margin_decomposition_adds_up(conn):
    m = frame(conn, """select margin_collected, price_gap, waste_cost, patient_resp, denied_leak, pending_leak, pricing_gap
                       from mart.drug_margin""").astype(float)
    rhs = m.price_gap - m.waste_cost - m.patient_resp - m.denied_leak - m.pending_leak - m.pricing_gap
    assert (m.margin_collected - rhs).abs().max() < 0.011


@pytest.mark.skipif(not (GEN / "drug_lot_truth.csv").exists(), reason="generator records absent")
def test_drug_totals_match_generator_lot_records(conn):
    lots = pd.read_csv(GEN / "drug_lot_truth.csv")
    pos = frame(conn, """select lot_no, purchased_units::float8 pu, administered_units::float8 au,
                                wasted_units::float8 wu, on_hand_units::float8 ou from mart.drug_lot_position""")
    m = lots.merge(pos, on="lot_no", how="inner")
    assert len(m) == len(lots) == len(pos)
    assert (m.units == m.pu).all()
    g = m.groupby("hcpcs")[["administered_units", "au", "wasted_units", "wu", "remaining_units", "ou"]].sum()
    assert (g.administered_units == g.au).all() and (g.wasted_units == g.wu).all() and (g.remaining_units == g.ou).all()
    # FIFO differs from the generator's first-expiry-first-out flow on only a few lots
    exact = ((m.administered_units == m.au) & (m.wasted_units == m.wu) & (m.remaining_units == m.ou)).mean()
    assert exact > 0.95


def test_breakeven_column_matches_formula(conn):
    from recon_engine.models.drug import breakeven_denial_rate, margin_at
    m = frame(conn, """select payer_paid, expected_allowed, denied_leak, fifo_cogs + waste_cost as cost,
                              breakeven_denial_share from mart.drug_margin where cohort = 'MATURE'""").astype(float)
    py = [breakeven_denial_rate(r.payer_paid, r.expected_allowed, r.denied_leak, r.cost) for r in m.itertuples()]
    assert (abs(m.breakeven_denial_share - py) < 1e-4).all()
    at_star = margin_at(m.payer_paid, m.expected_allowed, m.denied_leak, m.cost, 1, 1, denial_rate=m.breakeven_denial_share)
    assert (abs(at_star) < 0.001 * m.expected_allowed).all()
