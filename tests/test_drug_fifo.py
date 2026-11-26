"""FIFO lot costing (sql/models/61_drug_fifo.sql) on a hand-made example that can be checked on paper.

Drug X, as-of 2024-06-30.

    lot  received    expiry      units  invoice value  unit cost
    L1   2024-01-01  2024-12-31  10     $20.00         $2.00
    L2   2024-01-10  2024-02-29  10     $10.00         $1.00
    L3   2024-02-01  2025-06-30   3     $10.00         $3.333...

    event  date        type            units  FIFO lots used                cost
    E1     2024-01-05  ADMINISTER      6      L1 x 6                        $12.00
    E2     2024-01-15  ADMINISTER      7      L1 x 4 + L2 x 3 (partial lot) $8.00 + $3.00
    W1     2024-03-01  WASTE_EXPIRED   7      L2 x 7 (its own lot expired)  $7.00
    E4     2024-03-05  ADMINISTER      1      L3 x 1                        $3.33
    E5     2024-03-06  ADMINISTER      1      L3 x 1                        $3.34

L3 pieces get cumulative rounding: round(10 * 1/3) = 3.33, round(10 * 2/3) - 3.33 = 3.34, and the
unit left on hand carries 10.00 - 6.67 = 3.33, so the lot ties to the cent.
"""
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from recon_engine import db  # noqa: E402

DBNAME = "rre_test_drug_fifo"
FIFO_SQL = (ROOT / "sql" / "models" / "61_drug_fifo.sql").read_text()

LOTS = [  # lot_key, hcpcs, lot_no, invoice_no, received, expiry, units, value
    (1, "X", "L1", "INV1", "2024-01-01", "2024-12-31", 10, "20.00"),
    (2, "X", "L2", "INV2", "2024-01-10", "2024-02-29", 10, "10.00"),
    (3, "X", "L3", "INV3", "2024-02-01", "2025-06-30", 3, "10.00"),
]
EVENTS = [  # event_id, hcpcs, ts, type, lot_no, units
    ("E1", "X", "2024-01-05 09:00", "ADMINISTER", "L1", 6),
    ("E2", "X", "2024-01-15 09:00", "ADMINISTER", None, 7),
    ("W1", "X", "2024-03-01 07:00", "WASTE_EXPIRED", "L2", 7),
    ("E4", "X", "2024-03-05 09:00", "ADMINISTER", "L3", 1),
    ("E5", "X", "2024-03-06 09:00", "ADMINISTER", "L3", 1),
]


@pytest.fixture(scope="module")
def conn():
    db.drop_database(DBNAME)
    c = db.connect(DBNAME, autocommit=True)
    c.execute("create schema if not exists mart")
    yield c
    c.close()
    db.drop_database(DBNAME)


def load(conn, lots, events, as_of="2024-06-30"):
    conn.execute("drop table if exists mart.drug_fifo_param, mart.drug_fifo_lot_input, mart.drug_fifo_event_input")
    conn.execute("create table mart.drug_fifo_param (as_of date, mature_cutoff date)")
    conn.execute("insert into mart.drug_fifo_param values (%s, %s)", (as_of, as_of))
    conn.execute("""create table mart.drug_fifo_lot_input (lot_key bigint, hcpcs text, lot_no text, invoice_no text,
                    received_date date, expiry_date date, units numeric, lot_value numeric(14,2))""")
    conn.execute("""create table mart.drug_fifo_event_input (event_id text, hcpcs text, event_ts timestamp,
                    event_type text, lot_no text, units numeric, claim_line_id bigint, source text)""")
    with conn.cursor() as cur:
        cur.executemany("insert into mart.drug_fifo_lot_input values (%s,%s,%s,%s,%s,%s,%s,%s)", lots)
        cur.executemany("insert into mart.drug_fifo_event_input values (%s,%s,%s,%s,%s,%s,null,'TEST')", events)
    conn.execute(FIFO_SQL)


def alloc(conn):
    return conn.execute("""select event_id, lot_no, qty, cost from mart.drug_fifo
                           order by event_ts, event_id, lot_no""").fetchall()


def positions(conn):
    rows = conn.execute("""select lot_no, administered_units, administered_cost, wasted_units, wasted_cost,
                                  on_hand_units, on_hand_value, units_identity_ok, value_identity_ok, infeasible_units
                           from mart.drug_lot_position order by lot_no""").fetchall()
    return {r[0]: r[1:] for r in rows}


def D(x):
    return Decimal(str(x))


def test_fifo_allocation_by_hand(conn):
    load(conn, LOTS, EVENTS)
    got = [(e, l, float(q), c) for e, l, q, c in alloc(conn)]
    assert got == [
        ("E1", "L1", 6.0, D("12.00")),
        ("E2", "L1", 4.0, D("8.00")),     # partial lot: the rest of L1 ...
        ("E2", "L2", 3.0, D("3.00")),     # ... then the next lot in receipt order
        ("W1", "L2", 7.0, D("7.00")),     # expiry charged to the lot that expired
        ("E4", "L3", 1.0, D("3.33")),
        ("E5", "L3", 1.0, D("3.34")),
    ]


def test_lot_positions_and_identity(conn):
    load(conn, LOTS, EVENTS)
    p = positions(conn)
    assert p["L1"][:6] == (D(10), D("20.00"), D(0), D("0.00"), D(0), D("0.00"))
    assert p["L2"][:6] == (D(3), D("3.00"), D(7), D("7.00"), D(0), D("0.00"))
    assert p["L3"][:6] == (D(2), D("6.67"), D(0), D("0.00"), D(1), D("3.33"))
    # purchased = administered + wasted + on hand, to the unit and to the cent, every lot
    assert all(v[6] and v[7] for v in p.values())
    assert all(v[8] == 0 for v in p.values())          # every allocation is physically possible here
    assert conn.execute("select count(*) from mart.drug_fifo_unallocated").fetchone()[0] == 0


def test_future_data_cannot_change_earlier_allocations(conn):
    load(conn, LOTS, EVENTS)
    before = alloc(conn)
    later_lot = (4, "X", "L4", "INV4", "2024-07-01", "2025-12-31", 5, "50.00")
    later_events = [("E6", "X", "2024-07-15 09:00", "ADMINISTER", None, 2),
                    ("E7", "X", "2024-08-01 09:00", "ADMINISTER", None, 3)]
    # same snapshot date: rows dated after it are ignored
    load(conn, LOTS + [later_lot], EVENTS + later_events, as_of="2024-06-30")
    assert alloc(conn) == before
    # later snapshot: the new rows are costed, the earlier allocations do not move
    load(conn, LOTS + [later_lot], EVENTS + later_events, as_of="2024-12-31")
    after = alloc(conn)
    assert after[: len(before)] == before
    assert [(e, l, float(q)) for e, l, q, _ in after[len(before):]] == [("E6", "L3", 1.0), ("E6", "L4", 1.0), ("E7", "L4", 3.0)]


def test_infeasible_allocation_is_flagged(conn):
    # lot B arrives later but expires first; FIFO (by receipt) uses A first, so B is used after expiry
    lots = [(1, "Y", "A", "I1", "2024-01-01", "2024-12-31", 10, "10.00"),
            (2, "Y", "B", "I2", "2024-01-02", "2024-01-31", 10, "10.00")]
    events = [("e1", "Y", "2024-01-10 09:00", "ADMINISTER", "B", 10),
              ("e2", "Y", "2024-02-15 09:00", "ADMINISTER", "A", 10)]
    load(conn, lots, events)
    p = positions(conn)
    assert p["B"][8] == 10 and p["A"][8] == 0
    assert p["A"][6] and p["B"][6]


def test_more_dispensed_than_purchased_goes_to_unallocated(conn):
    lots = [(1, "Z", "A", "I1", "2024-01-01", "2024-12-31", 5, "5.00")]
    events = [("z1", "Z", "2024-01-10 09:00", "ADMINISTER", None, 8)]
    load(conn, lots, events)
    r = conn.execute("select event_id, unallocated_units from mart.drug_fifo_unallocated").fetchall()
    assert r == [("z1", D(3))]


# ---------------------------------------------------------------- break-even final-denial rate
def test_breakeven_denial_rate_by_hand():
    """Contract value E = 100, of which D = 20 finally denied; payer paid P = 72 on the other 80
    (k = 0.9, the rest is patient share). Cost C = 60.
    margin(d) = 0.9 * 100 * (1 - d) - 60 = 0  ->  d* = 1 - 60/90 = 1/3."""
    from recon_engine.models.drug import breakeven_denial_rate, margin_at
    d_star = breakeven_denial_rate(paid=72.0, expected=100.0, denied=20.0, cost=60.0)
    assert d_star == pytest.approx(1 / 3)
    # at the observed denial rate the counterfactual reproduces the observed margin P - C = 12
    assert margin_at(72.0, 100.0, 20.0, 60.0, 10, 8.0) == pytest.approx(12.0)
    assert margin_at(72.0, 100.0, 20.0, 60.0, 10, 8.0, denial_rate=d_star) == pytest.approx(0.0)
    # contract at 125% of a $8 reference on 10 units = E' = 100: same as observed
    assert margin_at(72.0, 100.0, 20.0, 60.0, 10, 8.0, contract_pct=1.25) == pytest.approx(12.0)
    # a drug that costs more than full collection can never break even through denials alone
    assert breakeven_denial_rate(72.0, 100.0, 20.0, 95.0) < 0
