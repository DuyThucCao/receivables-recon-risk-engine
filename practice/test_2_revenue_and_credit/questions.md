# Technical test 2: Revenue-cycle and credit analyst

**Time: 2 hours. Four questions.** Data: the built database of this repository (simulated
hospital, six payers, as-of date 2026-03-31). Tables you will need: `core.fact_remit_line`,
`core.fact_remit_adjustment`, `core.dim_reason_code`, `recon.remit_duplicate`,
`recon.claim_remit_match`, `recon.claim_line_balance`, `core.fact_claim_line`.

## Q1. Denial mix (SQL, about 25 minutes)

For each payer, show denied lines and denied dollars by denial category, each category's share of
that payer's denied dollars, and its rank within the payer. Exclude reversals (they are negative
copies of earlier postings), provider-level rows and lines excluded as file duplicates. Then list
denial dollars by adjustment reason code (group-reason, e.g. CO-197) with the code's description.

## Q2. Speed of payment (SQL, about 30 minutes)

For each payer, compute the days from claim submission to **first** payment for every paid claim
line. Report the median, the 90th percentile and the share paid within 30 days, separately for
lines paid first time (never denied) and lines paid only after a denial.

## Q3. A first borrowing base (SQL + analysis, about 40 minutes)

A lender considers a revolving facility secured by the hospital's receivables. Using the state of
the books on **2025-09-30** (postings dated on or before that day only):

- value each open line at contract expected allowed minus patient share minus paid to date (never
  below zero);
- lines older than 120 days since submission are ineligible; denied lines are ineligible;
- no payer may exceed 30% of the final eligible base (concentration limit);
- the advance rate is 85%.

Report gross value, ineligible amounts and eligible value per payer, then the eligible base after
the concentration limit and the availability. Explain in two sentences why the concentration limit
is a fixed-point problem and not a single cap.

## Q4. Note to the credit committee (write-up, about 25 minutes)

In at most 200 words: is this receivables book bankable on these numbers? Name the two biggest
risks to the lender, one data check you would insist on before funding, and whether 85% looks
right given that denied claims are frequent. State that the data are simulated.
