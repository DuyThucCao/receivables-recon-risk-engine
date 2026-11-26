"""Build the database from the raw documents and run the daily close.

Order of a full build (``build``):

1. run every ``sql/0*.sql`` file (schemas, raw tables, parse functions, reference data and rules);
2. land the seven raw files from ``data/raw`` into ``raw.*`` (all text, with file and row number);
3. run the remaining ``sql/*.sql`` files in name order. Files 1x-3x stage, model, reconcile and
   build the question-1 marts; files 4x and later belong to the analysis modules;
4. call ``recon.close_day(d)`` for every calendar day from the first bank posting to the as-of date.

``ingest_and_reconcile`` is the incremental entry point used by the app: it lands new rows
(remittance batch, bank statement, or one manually entered claim), reruns staging, core and
reconciliation on what is new, and returns the matches and the exceptions that appeared.

This module reads only ``data/raw`` and the configuration. It never opens the evaluation folder
written by the generator; a test enforces that.
"""
from __future__ import annotations

import csv
import io
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
import psycopg

from . import RAW_DIR, SQL_DIR
from . import db

RAW_TABLES: dict[str, str] = {
    "claims_export.csv": "raw.claims_export",
    "remittance_advice.csv": "raw.remittance_advice",
    "bank_statement.csv": "raw.bank_statement",
    "payer_contracts.csv": "raw.payer_contracts",
    "drug_purchase_invoices.csv": "raw.drug_purchase_invoices",
    "drug_dispense_log.csv": "raw.drug_dispense_log",
    "claim_status_history.csv": "raw.claim_status_history",
}


# ------------------------------------------------------------------------------------------
# SQL files
# ------------------------------------------------------------------------------------------
def sql_files(min_prefix: str = "", max_prefix: str = "9", sql_dir: Path = SQL_DIR) -> list[Path]:
    """SQL files whose first character lies in [min_prefix, max_prefix], in name order."""
    out = []
    for p in sorted(sql_dir.glob("*.sql")):
        first = p.name[0]
        if (not min_prefix or first >= min_prefix) and first <= max_prefix:
            out.append(p)
    return out


def run_sql_file(conn: psycopg.Connection, path: Path) -> None:
    conn.execute(path.read_text(encoding="utf-8"))


# ------------------------------------------------------------------------------------------
# landing raw rows
# ------------------------------------------------------------------------------------------
def table_columns(conn: psycopg.Connection, table: str) -> list[str]:
    schema, name = table.split(".")
    rows = conn.execute(
        """select column_name from information_schema.columns
           where table_schema = %s and table_name = %s and column_name not like '\\_%%'
           order by ordinal_position""", (schema, name)).fetchall()
    return [r[0] for r in rows]


def next_row_no(conn: psycopg.Connection, table: str, file_label: str) -> int:
    r = conn.execute(f"select coalesce(max(_row_no), 0) from {table} where _file = %s", (file_label,)).fetchone()
    return int(r[0])


def land_rows(conn: psycopg.Connection, table: str, header: list[str], rows: Iterable[list],
              file_label: str) -> int:
    """COPY rows (lists of strings, in ``header`` order) into a raw table. Unknown columns are an
    error; columns missing from the header are loaded as NULL. Row numbers continue after any rows
    already landed under the same file label."""
    cols = table_columns(conn, table)
    header = [h.strip().lower() for h in header]
    unknown = [h for h in header if h not in cols]
    if unknown:
        raise ValueError(f"{table}: unexpected columns {unknown}")
    idx = [header.index(c) if c in header else None for c in cols]
    start = next_row_no(conn, table, file_label)
    n = 0
    with conn.cursor() as cur:
        with cur.copy(f"copy {table} (_file, _row_no, {', '.join(cols)}) from stdin") as cp:
            for i, row in enumerate(rows, start=start + 1):
                cp.write_row([file_label, i] + [(row[j] if j is not None and row[j] != "" else None) for j in idx])
                n += 1
    return n


def land_csv(conn: psycopg.Connection, path: Path, table: str, file_label: str | None = None) -> int:
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        return land_rows(conn, table, header, reader, file_label or path.name)


def land_frame(conn: psycopg.Connection, df: pd.DataFrame, table: str, file_label: str) -> int:
    df = df.astype(object).where(pd.notna(df), "")
    rows = ([("" if v is None else str(v)) for v in rec] for rec in df.itertuples(index=False, name=None))
    return land_rows(conn, table, list(df.columns), rows, file_label)


def load_raw_dir(conn: psycopg.Connection, raw_dir: Path = RAW_DIR) -> dict[str, int]:
    counts = {}
    for name, table in RAW_TABLES.items():
        p = Path(raw_dir) / name
        if not p.exists():
            raise FileNotFoundError(f"raw file missing: {p}")
        counts[name] = land_csv(conn, p, table, name)
    return counts


def set_run_parameters(conn: psycopg.Connection, params: dict[str, Any]) -> None:
    for k, v in params.items():
        conn.execute("""insert into recon.run_parameter (name, value) values (%s, %s)
                        on conflict (name) do update set value = excluded.value""", (k, str(v)))


# ------------------------------------------------------------------------------------------
# daily close
# ------------------------------------------------------------------------------------------
def close_days(conn: psycopg.Connection, start: date | None = None, end: date | None = None,
               only_open: bool = False) -> dict[str, Any]:
    """Call recon.close_day for each calendar day in order. A refusal is logged and stops the
    sequence (later days cannot close before an earlier one)."""
    if start is None or end is None:
        r = conn.execute("select min(recon_date), max(recon_date) from recon.daily_recon").fetchone()
        start = start or r[0]
        end = end or r[1]
    closed, refused = 0, None
    d = start
    conn.commit()
    while d is not None and d <= end:
        if only_open:
            st = conn.execute("select status from recon.daily_recon where recon_date = %s", (d,)).fetchone()
            if st and st[0] == "CLOSED":
                d += timedelta(days=1)
                continue
        try:
            conn.execute("call recon.close_day(%s)", (d,))
            conn.commit()
            closed += 1
        except psycopg.Error as e:
            conn.rollback()
            msg = (e.diag.message_primary or str(e)).strip()
            conn.execute("insert into recon.day_close_log (recon_date, outcome, message) values (%s, 'REFUSED', %s)",
                         (d, msg))
            conn.commit()
            refused = {"date": d.isoformat(), "message": msg}
            break
        d += timedelta(days=1)
    return {"closed": closed, "refused": refused, "start": str(start), "end": str(end)}


# ------------------------------------------------------------------------------------------
# full build
# ------------------------------------------------------------------------------------------
def build(raw_dir: Path = RAW_DIR, *, database: str | None = None, run_params: dict[str, Any] | None = None,
          max_prefix: str = "9", close: bool = True, verbose: bool = True) -> dict[str, Any]:
    """Rebuild the database from nothing. Idempotent: schemas are dropped and recreated."""
    timings: dict[str, float] = {}
    t0 = time.time()

    def log(msg: str) -> None:
        if verbose:
            print(f"[pipeline {time.time() - t0:6.1f}s] {msg}", flush=True)

    with db.connect(database) as conn:
        for p in sql_files(max_prefix="0"):
            run_sql_file(conn, p)
        if run_params:
            set_run_parameters(conn, run_params)
        conn.commit()
        t = time.time()
        counts = load_raw_dir(conn, Path(raw_dir))
        conn.commit()
        timings["land_raw"] = round(time.time() - t, 2)
        log(f"landed raw files {counts}")
        for p in sql_files(min_prefix="1", max_prefix=max_prefix):
            t = time.time()
            run_sql_file(conn, p)
            conn.commit()
            timings[p.name] = round(time.time() - t, 2)
            log(f"ran {p.name} ({timings[p.name]}s)")
        close_result = None
        if close and max_prefix >= "3":
            t = time.time()
            close_result = close_days(conn)
            timings["close_days"] = round(time.time() - t, 2)
            log(f"close_day: {close_result}")
    timings["total"] = round(time.time() - t0, 2)
    return {"raw_rows": counts, "timings_s": timings, "close": close_result}


# ------------------------------------------------------------------------------------------
# incremental intake (used by the app)
# ------------------------------------------------------------------------------------------
def _as_frame(obj: Any) -> pd.DataFrame | None:
    if obj is None:
        return None
    if isinstance(obj, pd.DataFrame):
        return obj
    if isinstance(obj, (str, Path)):
        return pd.read_csv(obj, dtype=str, keep_default_na=False)
    if isinstance(obj, dict):
        return pd.DataFrame([obj])
    if isinstance(obj, list):
        return pd.DataFrame(obj)
    raise TypeError(f"cannot turn {type(obj)} into rows")


def ingest_and_reconcile(conn: psycopg.Connection | None = None, *, remittance: Any = None,
                         bank_statement: Any = None, claim: Any = None, batch_label: str | None = None,
                         refresh_downstream: bool = False, close: bool = True) -> dict[str, Any]:
    """Land a new remittance batch and/or bank statement (DataFrame or CSV path) and/or one
    manually entered claim (dict of claims_export columns, one dict per line or a list of them),
    rerun staging, core and reconciliation incrementally, and report what changed.

    Returns a dict with ``landed`` (rows per raw table), ``rejects`` (staging rejects of the new
    rows), ``claim_matches`` and ``bank_matches`` (matches involving the new rows),
    ``new_exceptions`` (exception-queue items that were not open before), and ``close``.
    """
    own = conn is None
    conn = conn or db.connect()
    label = batch_label or f"intake_{time.strftime('%Y%m%dT%H%M%S')}"
    try:
        before = {r[0] for r in conn.execute("select exception_key from recon.exception_queue where status = 'OPEN'")}
        landed = {}
        for obj, table in ((claim, "raw.claims_export"), (remittance, "raw.remittance_advice"),
                           (bank_statement, "raw.bank_statement")):
            df = _as_frame(obj)
            if df is not None and len(df):
                landed[table] = land_frame(conn, df, table, f"{label}:{table.split('.')[1]}")
        conn.execute("select stg.process_new()")
        conn.execute("select core.process_new()")
        conn.execute("select recon.run_all('INCREMENTAL')")
        conn.execute("select mart.refresh_q1()")
        if refresh_downstream:
            for p in sql_files(min_prefix="4"):
                run_sql_file(conn, p)
        conn.commit()
        like = f"{label}:%"
        cur = conn.cursor()

        def frame(q: str, params: tuple) -> pd.DataFrame:
            cur.execute(q, params)
            cols = [d.name for d in cur.description]
            return pd.DataFrame(cur.fetchall(), columns=cols)

        rejects = frame("""select source_table, src_row_no, reason_code, reason_detail from stg.reject
                           where src_file like %s order by source_table, src_row_no""", (like,))
        claim_matches = frame("""
            select r.src_file, r.src_row_no, r.patient_control_no_raw, r.trace_no, r.paid_amount,
                   m.rule_id, c.claim_id, c.line_no, b.status as line_status
            from core.fact_remit_line r
            left join recon.claim_remit_match m on m.remit_line_id = r.remit_line_id
            left join core.fact_claim_line c on c.claim_line_id = m.claim_line_id
            left join recon.claim_line_status b on b.claim_line_id = c.claim_line_id
            where r.src_file like %s or c.src_file like %s
            order by r.src_file, r.src_row_no""", (like, like))
        bank_matches = frame("""
            select b.src_row_no, b.posting_date, b.description, b.amount, m.trace_key, m.allocated_amount, m.rule_id
            from core.fact_bank_txn b left join recon.remit_bank_match m on m.bank_txn_id = b.bank_txn_id
            where b.src_file like %s
               or m.trace_key in (select trace_key from core.fact_remit_line where src_file like %s)
            order by b.src_row_no""", (like, like))
        new_exc = frame("""select exception_type, severity, claim_id, trace_key, amount, reason, exception_key
                           from recon.exception_queue where status = 'OPEN' order by exception_id""", ())
        new_exc = new_exc[~new_exc["exception_key"].isin(before)].reset_index(drop=True)
        close_result = close_days(conn, only_open=True) if close else None
        return {"batch_label": label, "landed": landed, "rejects": rejects, "claim_matches": claim_matches,
                "bank_matches": bank_matches, "new_exceptions": new_exc, "close": close_result}
    finally:
        if own:
            conn.close()
