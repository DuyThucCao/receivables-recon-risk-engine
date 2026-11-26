# Test 2: solution notes

SQL: `solutions/q1_denial_mix.sql`, `q2_days_to_payment.sql`, `q3_borrowing_base.sql`. Run them
with `practice/run_solutions.py test_2 --show 10`.

## Q1

- Shares of a grouped total need a window over an aggregate:
  `sum(denied_amount) / sum(sum(denied_amount)) over (partition by payer_id)`.
- Forgetting to exclude reversals double-counts nothing but *subtracts* nothing either: a reversal
  of a denial carries a negative denied amount, so filtering `denied_amount > 0` already drops it.
  Filter `not is_reversal` anyway, to make the intent explicit.
- File duplicates (`recon.remit_duplicate`) were never posted; including them overstates denials.
- In the simulation, administrative and prior-authorisation codes carry the most denied dollars.
  The mix is a generator setting, not a finding about real payers.

## Q2

- `percentile_cont(...) within group (order by ...)` gives the median and p90 per group.
- Lines paid after a denial take far longer; the gap between the two paths is the cost of a
  denial in days. The econ model measures the same thing in dollars of float: <!--n:q7.float_late-->$179,197.70<!--/n--> of
  late float in total, much of it on claims paid only after a denial.
- This simple version ignores lines still unpaid at the as-of date. The cash model's
  Kaplan-Meier curves keep them as right-censored observations, which is the correct treatment
  when many lines are still open.

## Q3

- Point in time again: postings dated by 2025-09-30 only.
- Denied lines: in this simple version a posted denial settles the line, so it drops out at the
  open-balance filter (the `open_lines_with_denial` column shows how many open lines carry one).
  The project's credit model values workable denials from the claim status feed and excludes
  them explicitly, which is better practice.
- Fixed point: each payer is capped at 30% of the *final* base E, but E itself shrinks when a
  payer is capped, which can push another payer over 30%. So E solves E = sum over payers of
  min(e_p, 0.3 E); the recursive CTE iterates it to convergence. In the build used for these notes no
  payer exceeds 30% before the cap, so the excess is zero; with fewer, larger payers it would bind.
- Compare with the project's certificate on the same date (<!--n:credit.certificate_date-->2025-09-30<!--/n-->): eligible
  <!--n:credit.eligible-->$3,204,908<!--/n-->, availability <!--n:credit.availability-->$2,656,726<!--/n-->. The project's number is lower because it values
  lines at net collectible value from payer collection ratios, excludes disputed (appealed or
  pended) claims and deducts a credit-balance reserve.

## Q4: model answer (simulated figures)

> **To:** Credit committee. **Re:** receivables facility, certificate <!--n:credit.certificate_date-->2025-09-30<!--/n--> (simulated
> data, fictional hospital).
>
> The book is bankable only with a lower advance rate. Eligible receivables are <!--n:credit.eligible-->$3,204,908<!--/n-->
> and availability at a <!--n:credit.advance_rate-->85%<!--/n--> advance rate is <!--n:credit.availability-->$2,656,726<!--/n-->. But expected loss on the eligible pool,
> <!--n:credit.expected_loss-->$662,340<!--/n-->, exceeds the lender's margin of <!--n:credit.lender_margin-->$480,736<!--/n-->: claims that look current are
> often denied later.
>
> Risks: (1) denial risk, which turns eligible collateral into write-offs; (2) dilution from
> payer recoupments and credit balances owed back. One check before funding: tie the borrower's
> aging to bank deposits for three months (a three-way reconciliation), because unapplied cash and
> payments with no deposit distort the aging. I would cut the advance rate or exclude claims from
> payers with high denial rates until the expected-loss check passes.
