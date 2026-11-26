# Ten SQL drills, easy to hard

Write each query yourself, then compare with `solutions/dNN_*.sql`. Time yourself: drills 1-3
about 10 minutes, 4-7 about 15, 8-10 about 30.

| # | Level | Skill | Task |
|---|---|---|---|
| 1 | easy | GROUP BY | Claim lines, distinct claims, billed total and average line charge per payer (`core.fact_claim_line`), largest billed first. |
| 2 | easy | HAVING | Claims with more than 5 lines; the 10 largest by total charge. Do it once with `core.v_claim` and once from the line table with `GROUP BY ... HAVING`. |
| 3 | easy | LEFT JOIN | For every claim-to-remittance rule in `recon.match_rule`, how many lines it matched (`recon.claim_remit_match`) and its share of all matches. Rules with no matches must still appear. |
| 4 | medium | RANK | For each service month, rank payers by underpaid dollars (`recon.claim_line_balance.underpaid_amount`) and keep the top 2. |
| 5 | medium | running SUM | Recompute the bank running balance from the opening balance and amounts in statement order (`core.fact_bank_txn`); count rows where it differs from the printed balance. Expect zero. |
| 6 | medium | CTE + ROW_NUMBER | Rebuild each claim line's current status as the last `status_after` in `recon.claim_line_status_history` (lines never posted stay SUBMITTED) and cross-tabulate it against `recon.claim_line_status.status`. Explain any off-diagonal cells. |
| 7 | medium | LEAD | For every DENIED event in `core.fact_claim_event`, find the next event of the same claim; average days to it and its status, by denial reason category. |
| 8 | hard | gaps and islands | (a) Runs of consecutive calendar weeks in which each payer sent at least one payment (`recon.remit_trace`); the longest run and the number of runs. (b) The longest gap between payment dates per payer. |
| 9 | hard | running FIFO | Allocate every administration of drug J9271 to purchase lots first-in-first-out in pure SQL, with cost per allocation; then prove every administered unit is allocated. |
| 10 | hard | daily close | Rebuild the bank side of `recon.daily_recon` (credits, debits, opening, closing) for every day from `core.fact_bank_txn` and show there are no differences. |

## Hints

- **4:** aggregate first in a CTE, then rank the aggregate. `RANK` ties share a rank.
- **5:** the opening balance is the first row's `running_balance - amount`. Use
  `sum(amount) over (order by bank_txn_id rows between unbounded preceding and current row)`.
- **6:** the off-diagonal cells come from statuses the history does not produce, such as
  `ORPHAN_REVERSAL`, which the core sets after the fact when a reversal negates a posting that
  never arrived.
- **8:** within one unbroken run, `week_start - 7 * row_number()` is constant; group by it.
- **9:** give each lot and each administration a range on the cumulative-units axis
  (`cum_end = running sum`, `cum_start = cum_end - units`); two ranges overlap when
  `a.start < l.end and l.start < a.end`, and the overlap is `least(ends) - greatest(starts)`. This is
  the same idea as the project's `sql/models/61_drug_fifo.sql`, which also charges expired units to
  their own lot and rounds cents so every lot ties exactly.
- **10:** `(array_agg(running_balance order by bank_txn_id desc))[1]` gives the last balance of
  the day. `recon.close_day` does this check, and more, before it closes a day.
