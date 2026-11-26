"""Sample inputs for the reconciliation desk, in the raw export formats of docs/data_dictionary.md.

Three kinds of input:

* ``fresh_batch`` builds a *new* payer payment from the database itself: a remittance (835-like)
  paying a few claims that have no remittance yet, and the bank statement rows on which the money
  arrives. It deliberately contains the defects the rules exist for, so every part of the desk has
  something to show: a mangled claim number (rule CR20), a line priced below contract (UNDERPAYMENT),
  a denied line, a line for a claim the hospital never billed (UNMATCHED_REMIT), and a payer credit in
  the bank with no remittance behind it (UNAPPLIED_CASH). All values are simulated.
* ``replay_samples`` returns the first rows of the original exports in ``data/samples/``. These rows
  were already received, so loading them again demonstrates duplicate detection.
* ``manual_claim_rows`` turns one hand-entered claim into ``claims_export`` rows.

Nothing here writes to the database; the desk passes the frames to
``recon_engine.pipeline.ingest_and_reconcile``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

import numpy as np
import pandas as pd

from .paths import SAMPLES_DIR

REMIT_COLUMNS = [
    "remit_file_id", "payer_id", "payer_name", "payer_tax_id", "payee_name", "payee_npi", "trace_no", "payment_method",
    "payment_date", "file_payment_amount", "patient_control_no", "payer_claim_control_no", "claim_status_code",
    "claim_charge_amount", "claim_paid_amount", "member_id", "service_date", "procedure_code", "modifier_1", "units",
    "line_item_control_no", "line_charge_amount", "line_allowed_amount", "line_paid_amount", "adj_group_1",
    "adj_reason_1", "adj_amount_1", "adj_group_2", "adj_reason_2", "adj_amount_2", "adj_group_3", "adj_reason_3",
    "adj_amount_3", "remark_code"]
BANK_COLUMNS = ["account_number", "posting_date", "value_date", "transaction_type", "description", "debit", "credit",
                "running_balance", "bank_reference", "customer_reference"]
CLAIM_COLUMNS = [
    "claim_id", "form_type", "bill_type", "facility_name", "facility_npi", "patient_account_no", "member_id",
    "payer_id", "payer_name", "service_line", "attending_provider_id", "statement_from_date", "statement_to_date",
    "submission_date", "claim_total_charge", "line_no", "line_item_control_no", "revenue_code", "procedure_code",
    "modifier_1", "ndc_code", "diagnosis_code_1", "diagnosis_code_2", "service_date", "units", "line_charge"]

PAYEE_NAME = "Meridian Health System"
PAYEE_NPI = "9000000017"
COINSURANCE = {"COMMERCIAL": 0.10, "SELF_FUNDED_TPA": 0.10, "GOVERNMENT": 0.0}


def _c(x) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _s(x: Decimal) -> str:
    return f"{x:.2f}"


def _money(x: Decimal) -> str:
    return f"{x:,.2f}"


@dataclass
class Batch:
    remittance: pd.DataFrame
    bank: pd.DataFrame
    notes: list[str] = field(default_factory=list)
    trace_no: str = ""
    payer_id: str = ""
    payment_total: Decimal = Decimal("0")


def _frame(conn, q: str, params: tuple = ()) -> pd.DataFrame:
    cur = conn.execute(q, params)
    return pd.DataFrame(cur.fetchall(), columns=[d.name for d in cur.description])


def as_of_date(conn) -> date:
    return date.fromisoformat(conn.execute("select recon.param('as_of_date')").fetchone()[0])


def open_claim_lines(conn) -> pd.DataFrame:
    """Claim lines on claims where every line still has no remittance (status SUBMITTED, not written
    off) and a contract price exists. Hand-entered claims (desk intake) come first."""
    return _frame(conn, """
        with ok as (
            select c.claim_id
            from core.fact_claim_line c
            join recon.claim_line_status s using (claim_line_id)
            join recon.claim_line_balance b using (claim_line_id)
            group by c.claim_id
            having bool_and(s.status = 'SUBMITTED' and not s.is_written_off and b.expected_allowed is not null)
        )
        select c.claim_line_id, c.claim_id, c.line_no, c.payer_id, p.payer_name, p.payer_type, p.payer_tax_id,
               p.ach_company_name, r.member_id, c.procedure_code, c.modifier_1, c.units, c.line_charge,
               c.line_item_control_no, c.service_date, c.submission_date, b.expected_allowed,
               (c.src_file <> 'claims_export.csv') as entered_on_desk
        from core.fact_claim_line c
        join ok using (claim_id)
        join recon.claim_line_balance b using (claim_line_id)
        join core.dim_payer p on p.payer_id = c.payer_id
        left join raw.claims_export r on r._file = c.src_file and r._row_no = c.src_row_no
        order by entered_on_desk desc, c.submission_date desc, c.claim_id, c.line_no""")


def _unused_trace(conn, rng: np.random.Generator) -> str:
    while True:
        t = str(int(rng.integers(1_000_000_000, 9_999_999_999)))
        if conn.execute("select 1 from core.fact_remit_line where trace_no = %s limit 1", (t,)).fetchone() is None:
            return t


def last_bank_balance(conn) -> Decimal:
    r = conn.execute("""select running_balance from core.fact_bank_txn
                        order by posting_date desc, bank_txn_id desc limit 1""").fetchone()
    return _c(r[0]) if r else Decimal("0")


def fresh_batch(conn, *, n_claims: int = 4, seed: int | None = None, payment_date: date | None = None,
                include_defects: bool = True) -> Batch:
    """A new remittance file and the matching bank statement rows, built from claims still open in
    ``conn``. Payment and bank posting are dated on the as-of date unless ``payment_date`` is given."""
    rng = np.random.default_rng(seed)
    lines = open_claim_lines(conn)
    if lines.empty:
        raise RuntimeError("no open claims without a remittance are left to pay; press Reset to restore the baseline")
    # one payer per payment: the payer of the first candidate (a hand-entered claim if there is one)
    payer = lines.iloc[0]["payer_id"]
    lines = lines[lines["payer_id"] == payer]
    claims = list(dict.fromkeys(lines["claim_id"]))[:n_claims]
    lines = lines[lines["claim_id"].isin(claims)]
    p = lines.iloc[0]
    pay_date = payment_date or as_of_date(conn)
    trace = _unused_trace(conn, rng)
    payer_num = payer[1:].zfill(2)
    file_id = f"ERA{payer_num}{pay_date:%Y%m%d}DSK{int(rng.integers(100, 999))}"
    coins = Decimal(str(COINSURANCE.get(p["payer_type"], 0.10)))
    notes: list[str] = []
    rows = []
    for k, claim in enumerate(claims):
        cl = lines[lines["claim_id"] == claim]
        pcn = claim
        role = "paid at contract"
        if include_defects and k == 1:
            pcn = f"PCN{claim.zfill(15)}"
            role = f"claim number printed as {pcn} (rule CR20 normalises it)"
        icn = f"{payer_num}{pay_date:%y%j}{int(rng.integers(1_000_000, 9_999_999))}"
        denied_claim = include_defects and k == 3
        claim_rows = []
        for j, (_, ln) in enumerate(cl.iterrows()):
            charge = _c(ln["line_charge"])
            allowed = min(_c(ln["expected_allowed"]), charge)
            row = {"patient_control_no": pcn, "payer_claim_control_no": icn, "member_id": ln["member_id"] or "",
                   "service_date": f"{ln['service_date']:%Y%m%d}", "procedure_code": ln["procedure_code"],
                   "modifier_1": ln["modifier_1"] or "", "units": f"{int(ln['units'])}" if float(ln["units"]).is_integer()
                   else str(ln["units"]), "line_item_control_no": ln["line_item_control_no"] or "",
                   "line_charge_amount": _s(charge), "remark_code": ""}
            if denied_claim:
                row.update(claim_status_code="4", line_allowed_amount="0.00", line_paid_amount="0.00",
                           adj_group_1="CO", adj_reason_1="197", adj_amount_1=_s(charge), remark_code="N381")
                paid = Decimal("0")
            else:
                if include_defects and k == 2 and j == 0:
                    allowed = _c(allowed * Decimal("0.92"))
                    role = f"line 1 priced at 92% of contract (underpaid by {_money(_c(ln['expected_allowed']) - allowed)})"
                pr = _c(allowed * coins)
                paid = allowed - pr
                row.update(claim_status_code="1", line_allowed_amount=_s(allowed), line_paid_amount=_s(paid),
                           adj_group_1="CO", adj_reason_1="45", adj_amount_1=_s(charge - allowed))
                if pr > 0:
                    row.update(adj_group_2="PR", adj_reason_2="2", adj_amount_2=_s(pr))
            row["_paid"] = paid
            claim_rows.append(row)
        if denied_claim:
            role = "denied: CO-197 prior authorisation absent"
        charge_tot = sum(_c(x) for x in cl["line_charge"])
        paid_tot = sum(r["_paid"] for r in claim_rows)
        for r in claim_rows:
            r.update(claim_charge_amount=_s(charge_tot), claim_paid_amount=_s(paid_tot))
        rows += claim_rows
        notes.append(f"{claim} ({len(cl)} line{'s' if len(cl) > 1 else ''}, billed {_money(charge_tot)}): {role}")
    if include_defects:
        ghost = f"MHS26{int(rng.integers(9_000_000, 9_999_999))}"
        amt = _c(round(float(rng.uniform(150, 600)), 2))
        rows.append({"patient_control_no": ghost, "payer_claim_control_no": f"{payer_num}{pay_date:%y%j}{int(rng.integers(1_000_000, 9_999_999))}",
                     "member_id": f"ZZ{int(rng.integers(100_000_000, 999_999_999))}", "service_date": f"{pay_date:%Y%m%d}",
                     "procedure_code": "99213", "modifier_1": "", "units": "1", "line_item_control_no": "",
                     "line_charge_amount": _s(_c(amt * Decimal('1.6'))), "remark_code": "", "claim_status_code": "1",
                     "line_allowed_amount": _s(amt), "line_paid_amount": _s(amt), "adj_group_1": "CO", "adj_reason_1": "45",
                     "adj_amount_1": _s(_c(amt * Decimal('1.6')) - amt), "claim_charge_amount": _s(_c(amt * Decimal('1.6'))),
                     "claim_paid_amount": _s(amt), "_paid": amt})
        notes.append(f"{ghost}: a claim number the hospital never billed (paid {_money(amt)}), goes to the exception queue")
    total = sum(r["_paid"] for r in rows)
    for r in rows:
        r.pop("_paid")
        r.update(remit_file_id=file_id, payer_id=payer, payer_name=str(p["payer_name"]).upper(),
                 payer_tax_id=p["payer_tax_id"], payee_name=PAYEE_NAME, payee_npi=PAYEE_NPI, trace_no=trace,
                 payment_method="ACH", payment_date=f"{pay_date:%Y%m%d}", file_payment_amount=_s(total))
    remit = pd.DataFrame(rows).reindex(columns=REMIT_COLUMNS).fillna("")

    bal = last_bank_balance(conn)
    bank_rows = []
    ref_base = f"FHB{pay_date:%y%m%d}D{int(rng.integers(10_000, 99_999))}"
    bal += total
    bank_rows.append({"account_number": "XXXXXX4821", "posting_date": f"{pay_date:%m/%d/%Y}", "value_date": f"{pay_date:%m/%d/%Y}",
                      "transaction_type": "ACH CREDIT",
                      "description": f"ACH CREDIT {p['ach_company_name']} HCCLAIMPMT TRN*1*{trace}*{p['payer_tax_id']}\\",
                      "debit": "", "credit": _money(total), "running_balance": _money(bal),
                      "bank_reference": f"{ref_base}1", "customer_reference": ""})
    notes.append(f"Bank: ACH credit {_money(total)} carrying trace {trace} (rule RB10 should match it to the remittance)")
    if include_defects:
        other = conn.execute("select ach_company_name from core.dim_payer where payer_id <> %s order by payer_id limit 1",
                             (payer,)).fetchone()[0]
        stray = _c(round(float(rng.uniform(1200, 4800)), 2)) + Decimal("0.37")
        bal += stray
        bank_rows.append({"account_number": "XXXXXX4821", "posting_date": f"{pay_date:%m/%d/%Y}",
                          "value_date": f"{pay_date:%m/%d/%Y}", "transaction_type": "ACH CREDIT",
                          "description": f"ACH CREDIT {other} HCCLAIMPMT", "debit": "", "credit": _money(stray),
                          "running_balance": _money(bal), "bank_reference": f"{ref_base}2", "customer_reference": ""})
        notes.append(f"Bank: ACH credit {_money(stray)} from {other} with no trace and no remittance (unapplied cash)")
    bank = pd.DataFrame(bank_rows).reindex(columns=BANK_COLUMNS).fillna("")
    return Batch(remit, bank, notes, trace, payer, total)


def replay_samples(n: int = 12) -> tuple[pd.DataFrame, pd.DataFrame]:
    """First ``n`` rows of the original remittance and bank exports (already received once)."""
    r = pd.read_csv(SAMPLES_DIR / "remittance_advice.csv", dtype=str, keep_default_na=False).head(n)
    b = pd.read_csv(SAMPLES_DIR / "bank_statement.csv", dtype=str, keep_default_na=False).head(max(2, n // 4))
    return r, b


def next_claim_id(conn, year2: str | None = None) -> str:
    year2 = year2 or f"{as_of_date(conn):%y}"
    prefix = f"MHS{year2}"
    r = conn.execute("select max(claim_id) from core.fact_claim_line where claim_id like %s", (prefix + "%",)).fetchone()[0]
    nxt = int(r[len(prefix):]) + 1 if r else 1
    return f"{prefix}{nxt:07d}"


def procedure_options(conn) -> pd.DataFrame:
    """Procedures with their service line, revenue code and the typical billed charge per unit."""
    return _frame(conn, """
        select c.procedure_code, p.service_line_code, p.revenue_code, p.is_drug,
               round(percentile_cont(0.5) within group (order by c.line_charge / nullif(c.units, 0))::numeric, 2) as typical_charge_per_unit,
               count(*) as n_lines
        from core.fact_claim_line c join core.dim_procedure p using (procedure_code)
        group by 1, 2, 3, 4 order by n_lines desc""")


def manual_claim_rows(*, claim_id: str, payer_id: str, payer_name: str, member_id: str, service_line: str,
                      service_date: date, submission_date: date, lines: list[dict], patient_account_no: str = "A00000000",
                      diagnosis_code: str = "Z00.00") -> pd.DataFrame:
    """claims_export rows for one claim; ``lines`` = [{procedure_code, revenue_code, units, line_charge}]"""
    total = sum(_c(l["line_charge"]) for l in lines)
    rows = []
    for i, l in enumerate(lines, start=1):
        rows.append({
            "claim_id": claim_id, "form_type": "UB04", "bill_type": "131", "facility_name": "Meridian General Hospital",
            "facility_npi": "9000000025", "patient_account_no": patient_account_no, "member_id": member_id,
            "payer_id": payer_id, "payer_name": payer_name, "service_line": service_line,
            "attending_provider_id": "PRV1001", "statement_from_date": f"{service_date:%m/%d/%Y}",
            "statement_to_date": f"{service_date:%m/%d/%Y}", "submission_date": f"{submission_date:%Y-%m-%d}",
            "claim_total_charge": _s(total), "line_no": str(i), "line_item_control_no": f"{claim_id}-{i:02d}",
            "revenue_code": str(l.get("revenue_code") or ""), "procedure_code": l["procedure_code"], "modifier_1": "",
            "ndc_code": "", "diagnosis_code_1": diagnosis_code, "diagnosis_code_2": "",
            "service_date": f"{service_date:%m/%d/%Y}", "units": str(l["units"]), "line_charge": _s(_c(l["line_charge"]))})
    return pd.DataFrame(rows).reindex(columns=CLAIM_COLUMNS)


def to_csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False, lineterminator="\n").encode("utf-8")
