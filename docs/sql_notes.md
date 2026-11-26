# SQL notes: the two heaviest reconciliation queries

Measured on the full build (72,256 claim lines, 89,041 remittance lines, 88,467 matches) with
`EXPLAIN (ANALYZE, BUFFERS)` on the embedded PostgreSQL 16, laptop, warm-ish cache, 2026-11-21.
Timings are single runs and only indicative.

Where the time goes in a full rebuild (about 70 s): staging 6 s, core 6 s, `recon.run_all` 6 s,
marts 0.4 s, and the `close_day` loop about 40 s (820 calls at about 50 ms each). Inside
`recon.run_all`, the two heaviest statements are the balance build and the state-machine build.

## 1. `recon.build_balances()`: control identity + contract expectation (about 1.3 s with the insert)

Shape: aggregate postings per claim line (`claim_remit_match` join `fact_remit_line`, group by
`claim_line_id`), left join to every claim line, then a LATERAL lookup of the applicable contract
rate (payer, code or ALL, effective period; code-specific first, latest period first, LIMIT 1).

Plan highlights (select part, 366 ms):
- Hash join of matches to remittance lines (88,467 rows, 49 ms), then a HashAggregate by
  `claim_line_id` that spilled to disk in 41 batches (8.4 MB memory, 7.2 MB disk) under the
  default `work_mem`.
- Hash right join to `fact_claim_line` (72,256 rows).
- The LATERAL contract lookup is wrapped in **Memoize**: 28,565 cache hits, 43,691 misses. Each miss
  is a BitmapOr of two index scans on `dim_contract_rate` (the unique key for scope = ALL, and the
  `(payer_id, procedure_code, effective_from)` index for scope = CODE), about 3 microseconds each.

Notes:
- The `(payer_id, procedure_code, effective_from)` index is what makes the lateral lookup cheap;
  without it each miss would scan the contract table.
- The aggregate spill could be removed with a larger `work_mem` for this session
  (`set local work_mem = '32MB'`); at this size it costs tens of milliseconds, so it was left.
- The `CHECK (billed = paid + contractual + denied + patient_resp + open_balance)` constraint is
  evaluated per inserted row; it is cheap and guards against later tampering (a test updates one
  cent and expects a `CheckViolation`).

## 2. `recon.build_line_status()`: claim-line state machine (about 1.4 s with inserts and the transition update)

Shape: number every posting per claim line (`row_number() over (partition by claim_line_id order by
payment_date, remit_line_id)`), compute running sums of paid, contractual, denied and patient
responsibility and a running `bool_or(is_reversal)` with a ROWS frame, derive the status after each
posting from the running balance, `lag()` it for the status before, then flag transitions not in
`recon.line_status_transition`.

Plan highlights (window part, 105 ms):
- Hash join of matches to remittance lines (45 ms).
- One Sort on `(claim_line_id, payment_date, remit_line_id)` feeding a single **WindowAgg** that
  computes all five running aggregates in one pass (the two windows share the same partition and
  order, so PostgreSQL sorts once). The sort spilled (external merge, 2.6 MB).
- Merge join to `claim_line_balance` via its primary key (index only scan).

Notes:
- Most of the 1.4 s is writing 88k history rows and the follow-up `UPDATE ... FROM
  line_status_transition`; folding the transition check into the INSERT with a left join would
  avoid rewriting every row (backlog candidate, small gain).
- The current-status step uses a LATERAL "last row per line" on the history primary key, which
  is an index lookup per line.

## 3. The close loop (largest wall-clock share)

### Profiling finding (2026-11-21): build time 78 s -> 506 s -> back down
A clean rebuild took 506 s, of which 486 s was the `close_day` loop (all SQL files finished at 20 s).
Two causes, both found by timing one `call recon.close_day('2025-06-03')`:

1. **No planner statistics.** `pg_stat_user_tables.last_autoanalyze` was empty for every `recon`
   table and for `core.fact_remit_adjustment` / `core.fact_claim_event`: they are truncated and
   refilled inside one function, and autovacuum had not reached them before the close loop started
   firing hundreds of small queries. One call took 0.156 s without statistics and 0.047 s after a
   plain `ANALYZE`. Fix: `recon.run_all()` now ends with explicit `ANALYZE` of the tables it rebuilt.
2. **Row-by-row work outside the requested day.** `recon.daily_numbers(d, d)` computed the deposit
   allocation with a correlated subquery for every bank row of the whole statement before filtering to
   the day. Fix: the previous-balance window still runs over the statement (it needs the prior row),
   but allocations come from one grouped join restricted to the requested days. Behaviour unchanged.

Contributing factor that the code cannot fix: three other workstreams were running full builds on
the same laptop at the same time, each with its own embedded server, so CPU contention inflated
every timing in that run. The build time after the fixes is recorded in `results/core_build.json`.

`recon.close_day(d)` deliberately recomputes the day from base tables (`recon.daily_numbers(d, d)`,
row-by-row balance chain, allocation sums, claim lines posted that day) instead of trusting stored
numbers, which is the point of the control. Each call is about 50 ms; 820 calls take about 40 s.
Indexes used: `fact_bank_txn (posting_date, bank_txn_id)`, `fact_remit_line (payment_date)`,
`remit_trace (payment_date)`, `claim_remit_match (claim_line_id)`, and the unique
`exception_queue (exception_key)` for "is this item queued" probes. The main remaining cost is that
`daily_numbers` computes the deposit allocation for every bank row before filtering to the day;
that CTE is now restricted to the requested days (see the profiling finding above).
