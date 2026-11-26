"""Control totals tie to the cent on the full build (run_all.py must have been run)."""
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from recon_engine import db  # noqa: E402


@pytest.fixture(scope="module")
def conn():
    c = db.connect()
    built = c.execute("select to_regclass('recon.control_totals')").fetchone()[0]
    if not built or not c.execute("select count(*) from recon.control_totals").fetchone()[0]:
        pytest.skip("database not built: run `python run_all.py` first")
    yield c
    c.close()


def test_every_control_total_passes_to_the_cent(conn):
    rows = conn.execute("select check_name, scope, expected, actual, difference, passed from recon.control_totals").fetchall()
    assert len(rows) >= 20
    bad = [r for r in rows if not r[5] or r[4] != Decimal("0.00")]
    assert not bad, bad


def test_identity_holds_on_every_line(conn):
    n = conn.execute("""select count(*) from recon.claim_line_balance
                        where billed <> paid + contractual + denied + patient_resp + open_balance""").fetchone()[0]
    assert n == 0


def test_identity_holds_in_total_against_raw(conn):
    raw, core, rej = conn.execute("""select (select sum(stg.parse_money(line_charge)) from raw.claims_export),
                                            (select sum(billed) from recon.claim_line_balance),
                                            (select coalesce(sum(amount), 0) from stg.reject where source_table = 'raw.claims_export')""").fetchone()
    assert raw == core + rej
    tot = conn.execute("select sum(billed), sum(paid + contractual + denied + patient_resp + open_balance) from recon.claim_line_balance").fetchone()
    assert tot[0] == tot[1]


def test_posted_money_equals_matched_remittances(conn):
    a, b = conn.execute("""select (select sum(paid) from recon.claim_line_balance),
                                  (select sum(r.paid_amount) from recon.claim_remit_match m join core.fact_remit_line r using (remit_line_id))""").fetchone()
    assert a == b


def test_cash_identity_both_sides(conn):
    checks = dict(conn.execute("select check_name, difference from recon.control_totals where check_name like 'CASH_%'").fetchall())
    assert checks == {"CASH_REMIT_SIDE": Decimal("0.00"), "CASH_BANK_SIDE": Decimal("0.00")}


def test_bank_statement_chains(conn):
    n = conn.execute("""select count(*) from (select running_balance, amount,
                        lag(running_balance) over (order by posting_date, bank_txn_id) prev from core.fact_bank_txn) t
                        where prev is not null and prev + amount <> running_balance""").fetchone()[0]
    assert n == 0


def test_every_day_closed(conn):
    st = dict(conn.execute("select status, count(*) from recon.daily_recon group by 1").fetchall())
    assert set(st) == {"CLOSED"}
    assert conn.execute("select count(*) from recon.day_close_log where outcome = 'REFUSED'").fetchone()[0] == 0


MONEY_WORDS = ("amount", "amt", "billed", "paid", "usd", "balance", "cost", "margin", "revenue", "price",
               "cash", "loss", "ncv", "eligible", "availability", "shortfall", "reserve", "ibnr", "recovery")


def test_money_columns_are_numeric_not_float(conn):
    # Pipeline layers hold no floating-point columns at all.
    rows = conn.execute("""select table_schema, table_name, column_name from information_schema.columns
                           where table_schema in ('stg', 'core', 'recon')
                             and data_type in ('real', 'double precision')""").fetchall()
    assert rows == []
    # Model marts may store probabilities, rates and day counts as floats, but never money.
    rows = conn.execute("""select table_name, column_name from information_schema.columns
                           where table_schema = 'mart' and data_type in ('real', 'double precision')""").fetchall()
    money = [(t, c) for t, c in rows
             if any(w in c for w in MONEY_WORDS) and not c.startswith(("p_", "pd", "lgd")) and "rate" not in c]
    assert money == []


def test_every_match_records_its_rule(conn):
    for t in ("recon.claim_remit_match", "recon.remit_bank_match", "recon.remit_duplicate"):
        assert conn.execute(f"select count(*) from {t} where rule_id is null").fetchone()[0] == 0
        assert conn.execute(f"select count(*) from {t} x left join recon.match_rule r using (rule_id) where r.rule_id is null").fetchone()[0] == 0


def test_mart_tables_are_labelled(conn):
    marts = {r[0] for r in conn.execute("select 'mart.' || table_name from information_schema.tables where table_schema = 'mart' and table_name <> 'table_label'")}
    labelled = {r[0] for r in conn.execute("select table_name from mart.table_label")}
    core_marts = {"mart.cash_position", "mart.cash_stuck", "mart.ar_aging", "mart.pnl_by_payer_service"}
    assert core_marts <= labelled
    assert core_marts <= marts


def test_unreadable_dispense_units_are_imputed_and_still_rejected(conn):
    rows = conn.execute("""select d.src_row_no, d.billing_units from core.fact_drug_dispense d where d.units_imputed""").fetchall()
    assert rows and all(u > 0 for _, u in rows)
    for row_no, _ in rows:
        assert conn.execute("""select count(*) from stg.reject where source_table = 'raw.drug_dispense_log'
                               and src_row_no = %s and reason_code = 'UNPARSEABLE_UNITS'""", (row_no,)).fetchone()[0] == 1


def test_orphan_reversal_lines_are_not_reported_as_partial(conn):
    n_flag, n_status = conn.execute("""select count(*) filter (where has_orphan_reversal),
                                              count(*) filter (where status = 'ORPHAN_REVERSAL') from recon.claim_line_status""").fetchone()
    assert n_flag == n_status
    bad = conn.execute("""select count(*) from recon.claim_line_status s join recon.claim_line_balance b using (claim_line_id)
                          where s.status = 'PARTIAL' and b.open_balance = b.billed and b.n_postings > 0""").fetchone()[0]
    assert bad == 0
