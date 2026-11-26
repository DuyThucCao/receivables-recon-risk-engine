# Test 1: solution notes

SQL: `solutions/q1_remit_vs_bank_by_month.sql`, `q2_unapplied_cash_candidates.sql`,
`q3_identity_and_aging.sql`. Run them with `practice/run_solutions.py test_1 --show 10`.

## Q1

- Aggregate `remit_bank_match` to one row per `trace_key` **before** joining, or a split payment
  is counted twice on the remittance side (fan-out). This is the most common mistake.
- The control statement returns `check_ok = true`: not-matched dollars equal the `REMIT_NO_DEPOSIT`
  plus `IN_TRANSIT` queue amounts. In this build there is nothing in transit, so the whole gap is
  <!--n:recon.remit_no_deposit.amount-->$1,596,219.08<!--/n--> on <!--n:recon.remit_no_deposit.n-->51<!--/n--> payments.
- A month flagged `REVIEW` is a reason to open the exception queue for that payer, not a
  conclusion.

## Q2

- Expected shape: many of the largest credits have **no** candidate. Some are deposits with no
  remittance at all (the payer's 835 file never arrived), so cash posting must request it; others
  were queued as AMBIGUOUS because two equal payments fit (the queue reason says which). Some show a `POSSIBLE SPLIT` candidate: a larger payment of the same payer on the
  same day.
- Should it be posted automatically? No. An equal amount or a nearby date is not proof; a wrong
  posting misstates claim balances and is hard to undo. The project's rules post only a unique,
  traceable match; everything else stays in the queue for a person with the payer portal. That is
  why bank-side recall is <!--n:recon.r2b.recall-->0.9616<!--/n--> while precision is <!--n:recon.r2b.precision-->1.0000<!--/n-->.

## Q3

- (a) Every payer row and the total show difference 0.00. Using `GROUP BY ROLLUP` gives the total
  row in the same query.
- (b) Point in time means two filters: claim lines submitted by the date, and postings dated by
  the date. Using `recon.claim_line_balance` directly would leak future payments into the past.
  A denial settles a line (denied is one of the identity's components), so denied dollars are not
  open balance; they belong in their own column.
- (c) The `check_ok` statement compares the undated rebuild with `recon.claim_line_balance`; it must
  be true. Writing this check is what separates a reconciliation analyst from a report writer.
- (d) A good answer names the payer with the highest share over 90 days and then lists what to
  check first: whether those lines are waiting for a remittance that never arrived (unapplied cash
  of the same payer), whether they carry orphan reversals, whether the claims were ever
  acknowledged (no-response items), and only then whether the payer is slow.

## Q4: model answer (simulated figures)

> **To:** CFO. **Re:** cash stuck at 2026-03-31 (simulated data set, fictional hospital).
>
> Three buckets need action. First, <!--n:recon.unapplied.amount-->$2,048,321.11<!--/n--> of payer credits (<!--n:recon.unapplied.n-->64<!--/n--> items) is not yet
> applied: <!--n:recon.unapplied.true-->12<!--/n--> items are deposits with no remittance at all, and the rest are queued for a
> person to match (two equal candidates, or a deposit that looks split). Claim balances look unpaid
> until it is posted. Second, <!--n:recon.remit_no_deposit.amount-->$1,596,219.08<!--/n--> on <!--n:recon.remit_no_deposit.n-->51<!--/n--> payments shows no matched deposit;
> only <!--n:recon.remit_no_deposit.true-->7<!--/n--> of those payments truly never reached the bank, the others wait on the same
> manual matching. Third, <!--n:recon.denied_in_play.amount-->$2,292,185.59<!--/n--> of denials can still be appealed or
> corrected.
>
> Actions for next week: (1) cash posting requests the missing 835 files for the ten largest
> unapplied credits from each payer; (2) treasury traces the largest payments with no deposit with
> the payers' banks; (3) revenue cycle works the denial queue in expected-value order before appeal
> windows close. Contracting should also review <!--n:recon.underpaid.n-->4,135<!--/n--> lines paid below contract.
>
> All books tie to the cent (<!--n:recon.control_checks-->24<!--/n--> control checks), so these are real gaps in the
> simulation, not reporting errors.
