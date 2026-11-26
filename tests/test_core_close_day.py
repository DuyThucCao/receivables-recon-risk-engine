"""recon.close_day refuses a day whose totals do not tie. Each corruption is rolled back."""
import sys
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from recon_engine import db  # noqa: E402


@pytest.fixture()
def conn():
    c = db.connect()
    if not c.execute("select to_regclass('recon.daily_recon')").fetchone()[0]:
        pytest.skip("database not built: run `python run_all.py` first")
    yield c
    c.rollback()
    c.close()


def busy_day(conn):
    """A day with matched EFT deposits, postings and payments."""
    return conn.execute("""select b.posting_date from core.fact_bank_txn b join recon.remit_bank_match m using (bank_txn_id)
                           join recon.remit_trace t using (trace_key)
                           where b.txn_category = 'PAYER_EFT' and t.payment_date = b.posting_date
                           order by b.posting_date limit 1 offset 20""").fetchone()[0]


def refuses(conn, day, fragment):
    with pytest.raises(psycopg.errors.RaiseException) as e:
        conn.execute("call recon.close_day(%s)", (day,))
    assert "refused" in str(e.value)
    assert fragment in str(e.value), str(e.value)


def test_clean_day_closes(conn):
    d = busy_day(conn)
    conn.execute("call recon.close_day(%s)", (d,))


def test_refuses_when_a_bank_amount_is_off_by_one_cent(conn):
    d = busy_day(conn)
    conn.execute("""update core.fact_bank_txn set amount = amount + 0.01
                    where bank_txn_id = (select bank_txn_id from core.fact_bank_txn where posting_date = %s
                                         and txn_category = 'PAYER_EFT' limit 1)""", (d,))
    refuses(conn, d, "running-balance chain")


def test_refuses_when_a_deposit_match_disappears(conn):
    d = busy_day(conn)
    conn.execute("""delete from recon.remit_bank_match where bank_txn_id = (
                        select b.bank_txn_id from core.fact_bank_txn b join recon.remit_bank_match m using (bank_txn_id)
                        where b.posting_date = %s limit 1)""", (d,))
    refuses(conn, d, "neither matched nor queued")


def test_refuses_when_a_claim_balance_is_tampered(conn):
    d = busy_day(conn)
    conn.execute("""update recon.claim_line_balance set paid = paid + 0.01, open_balance = open_balance - 0.01
                    where claim_line_id = (select m.claim_line_id from recon.claim_remit_match m join core.fact_remit_line r using (remit_line_id)
                                           where r.payment_date = %s limit 1)""", (d,))
    refuses(conn, d, "control identity or differ from their postings")


def test_refuses_when_an_allocation_is_partial(conn):
    d = busy_day(conn)
    conn.execute("""update recon.remit_bank_match set allocated_amount = allocated_amount - 0.01 where bank_txn_id = (
                        select b.bank_txn_id from core.fact_bank_txn b join recon.remit_bank_match m using (bank_txn_id)
                        where b.posting_date = %s limit 1)""", (d,))
    refuses(conn, d, "allocations do not sum")


def test_refuses_out_of_order(conn):
    d = busy_day(conn)
    conn.execute("update recon.daily_recon set status = 'OPEN' where recon_date = %s::date - 1", (d,))
    refuses(conn, d, "not CLOSED")


def test_identity_is_a_table_constraint(conn):
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("update recon.claim_line_balance set paid = paid + 0.01 where claim_line_id = (select min(claim_line_id) from recon.claim_line_balance)")
