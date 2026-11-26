"""CFO operations report: builds from the pipeline database and results/, has 3 to 5 pages, says the
data are simulated, and quotes numbers that tie to the marts (skips without a built database)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recon_engine.reports import cfo_report  # noqa: E402


def test_money_formatting():
    assert cfo_report.mm(2_292_185.59) == "$2.29 million"
    assert cfo_report.mm(324_590.51) == "$324,591"
    assert cfo_report.mm(-95_638.73) == "-$95,639"
    assert cfo_report.money(-1234.5, 2) == "-$1,234.50"
    assert cfo_report.pct(0.6858) == "68.6%"


def test_underscored_identifiers_are_set_in_a_font_that_has_the_glyph():
    s = cfo_report._u("Source: mart.pnl_by_payer_service " + cfo_report.lab("model_estimate"))
    assert '<font name="STIX">mart.pnl_by_payer_service</font>' in s
    assert s.count('name="STIX"') == 2          # the label tag is not wrapped twice


@pytest.fixture(scope="module")
def conn():
    from recon_engine import db
    try:
        c = db.connect()
        c.execute("select 1 from mart.cash_stuck limit 1")
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"no built database reachable: {e}")
    yield c
    c.close()


def test_gather_ties_to_the_database(conn):
    f = cfo_report.gather(conn)
    billed = float(conn.execute("select sum(billed) from recon.claim_line_balance").fetchone()[0])
    assert f["pnl"]["billed"] == pytest.approx(billed, abs=0.01)
    assert f["identity_residual"] == pytest.approx(0.0, abs=0.005)
    assert f["controls"]["all_passed"]
    stuck = dict(conn.execute("select bucket, amount from mart.cash_stuck").fetchall())
    for b, r in f["stuck_by"].items():
        assert r["amount"] == pytest.approx(float(stuck[b]), abs=0.005)


def test_report_builds_with_3_to_5_pages(conn, tmp_path):
    out = cfo_report.build(tmp_path / "cfo_report.pdf", conn=conn)
    assert out.exists() and out.stat().st_size > 20_000
    n = cfo_report.page_count(out)
    assert 3 <= n <= 5, n
    try:
        import pymupdf
    except ImportError:
        return
    text = " ".join(p.get_text() for p in pymupdf.open(out))
    assert "simulated" in text and "fictional" in text
    for head in ("1. Situation", "2. Where the cash is stuck", "3. What to do this month", "4. Risks and limits"):
        assert head in text
    assert "synthetic_record" in text and "model_estimate" in text
