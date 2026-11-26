# Technical test 1: Cash posting and reconciliation analyst

**Time: 2 hours. Four questions.** You may use any SQL reference. Submit your SQL and short
answers. Data: the built database of this repository (simulated hospital, six payers, one bank
account, as-of date 2026-03-31). Tables you will need: `recon.remit_trace`,
`recon.remit_bank_match`, `recon.exception_queue`, `core.fact_bank_txn`, `recon.claim_line_balance`,
`recon.claim_line_status`, `recon.claim_remit_match`, `core.fact_remit_line`,
`core.fact_claim_line`, `mart.cash_stuck`, `recon.run_parameter`.

## Q1. Remitted versus deposited (SQL, about 25 minutes)

For each payer and payment month, report the number of payments, the total the payer says it
paid (`remit_trace.header_total`), the amount matched to bank deposits, the amount not matched and
the match ratio. Flag months below 98% as `REVIEW`.

Then prove your not-matched total is fully explained by the exception queue (items
`REMIT_NO_DEPOSIT` plus `IN_TRANSIT`).

*Watch out:* one payment can be split across two deposits, so `remit_bank_match` can have several
rows per payment.

## Q2. Unapplied cash (SQL, about 30 minutes)

List the 10 largest open `UNAPPLIED_CASH` credits (bank date, payer, amount, queue reason). For
each, show candidate payments from the same payer that never found a deposit
(`REMIT_NO_DEPOSIT`), dated within 5 days, whose amount equals the credit (exact) or is larger
(possible split deposit). In two sentences: should a candidate be posted automatically? Why or why
not?

## Q3. Do the books tie, and how old is the open book? (SQL + analysis, about 35 minutes)

a. For each payer and in total, show that billed = paid + contractual + denied + patient
   responsibility + open balance holds to the cent.

b. Build an aging of open receivables **as it would have looked on 2025-09-30**, by payer, in
   buckets 0-30, 31-60, 61-90, 91-120 and over 120 days since submission. Use only remittance
   postings dated on or before 2025-09-30. Report the share over 90 days per payer and, separately,
   denied dollars to date.

c. Prove your point-in-time method is right: run the same rebuild with no date limit and show it
   equals the open balance in `recon.claim_line_balance`.

d. In three sentences: which payer's book looks worst, and what would you check before calling it
   a collection problem?

## Q4. Note to the CFO (write-up, about 30 minutes)

Using `mart.cash_stuck` and your results above, write at most 200 words to the CFO: how much cash
is stuck, in which buckets, and three actions for next week, each with an owner (cash posting,
revenue cycle, treasury, contracting). State clearly that the figures are from the simulated
data set.
