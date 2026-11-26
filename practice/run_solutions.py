#!/usr/bin/env python
"""Run every practice SQL solution against the built database, read-only, and report the result.

    python practice/run_solutions.py              # all files
    python practice/run_solutions.py drills       # path filter
    python practice/run_solutions.py --show 5     # print 5 rows each

Each file may hold several statements separated by a semicolon at the end of a line. Every
statement runs inside a READ ONLY transaction, so a solution can never change the database.
A statement whose first column is named ``check_ok`` must return only true values; that is how
the solutions assert their own control checks.

The database is the one ``src/recon_engine/db.py`` connects to: ``DATABASE_URL`` if set, else the
embedded server in ``RRE_PG_DIR`` (default ``~/.cache/receivables-engine/pg``). Build it first with
``run_all.py``.
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from recon_engine import db  # noqa: E402


def statements(sql: str) -> list[str]:
    body = "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--"))
    parts = re.split(r";\s*(?:\n|$)", body)
    return [p.strip() for p in parts if p.strip()]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("filter", nargs="?", default="")
    ap.add_argument("--show", type=int, default=0, help="print this many rows of each result")
    a = ap.parse_args()
    files = sorted(p for p in HERE.glob("**/*.sql") if a.filter in str(p.relative_to(HERE)))
    failures = 0
    with db.connect() as conn:
        for f in files:
            for i, stmt in enumerate(statements(f.read_text()), 1):
                t0 = time.time()
                try:
                    with conn.transaction():
                        conn.execute("set transaction read only")
                        cur = conn.execute(stmt)
                        rows = cur.fetchall() if cur.description else []
                        cols = [d.name for d in cur.description] if cur.description else []
                except Exception as e:  # noqa: BLE001
                    failures += 1
                    print(f"FAIL {f.relative_to(HERE)} [{i}]: {type(e).__name__}: {str(e).splitlines()[0]}")
                    continue
                status = "ok  "
                if cols and cols[0] == "check_ok" and not all(r[0] for r in rows):
                    status = "CHECK FAILED"
                    failures += 1
                print(f"{status} {f.relative_to(HERE)} [{i}]: {len(rows)} rows, {time.time() - t0:.2f} s")
                for r in rows[: a.show]:
                    print("      ", dict(zip(cols, r)))
    print(f"\n{len(files)} files, {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
