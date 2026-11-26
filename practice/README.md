# Practice: timed tests, SQL drills and domain questions on this schema

Use these the way an employer's technical test works: set a timer, close the solutions, write
your own SQL against the built database, then compare.

| Folder | What | Time |
|---|---|---|
| `test_1_cash_posting/` | Technical test 1, cash posting and reconciliation analyst: 3 SQL/analysis questions + 1 write-up | 2 hours |
| `test_2_revenue_and_credit/` | Technical test 2, revenue-cycle and credit analyst: 3 SQL/analysis questions + 1 write-up | 2 hours |
| `sql_drills/` | 10 drills from easy to hard: GROUP BY, joins, window functions, CTEs, gaps and islands, running FIFO, daily close | 10 to 30 min each |
| `domain/` | 5 domain questions (revenue cycle, remittance codes, borrowing base, IBNR, prompt pay), each with SQL | 15 min each |

## Setup

1. Build the database once: `python run_all.py`.
2. Open a SQL client on it. The simplest is Python:

   ```python
   import sys; sys.path.insert(0, "src")
   from recon_engine import db
   conn = db.connect()
   conn.execute("select count(*) from core.fact_claim_line").fetchone()
   ```

3. Read `docs/data_dictionary.md` for tables and columns. The schemas are `raw` (files as
   landed), `stg` (typed), `core` (facts and dimensions per business process), `recon` (matches,
   balances, exception queue, daily close) and `mart` (outputs).

## Checking the solutions

Every solution file runs read-only against the built database:

```bash
python practice/run_solutions.py            # all solutions
python practice/run_solutions.py drills --show 5
```

A statement whose first column is `check_ok` asserts a control (for example "the rebuilt open
balance equals `recon.claim_line_balance`"); the runner fails if it is false. All solutions were
run against the embedded database built on 2026-11-21 and returned without error.

All data are simulated for a fictional hospital. Query results describe the simulation, not real
payers.

## Rules for yourself

- Write the SQL before you look. Explain each join's grain out loud: one row per what?
- Always check a total against a control (a sum that must tie) before you trust a result.
- In the write-ups, state assumptions and limits first, then the recommendation.
