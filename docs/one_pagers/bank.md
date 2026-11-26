# Receivables Reconciliation & Risk Engine: Bank one-pager

**Summary.** A working three-way payment reconciliation (billed, remitted, deposited) in
PostgreSQL that ties to the cent, with a borrowing-base certificate and expected-loss check built
on the reconciled receivables. The core is industry-neutral; healthcare claims are the
demonstration data, simulated for a fictional hospital, six payers and one bank account.

## Business questions answered

- Did every payment that a payer reported actually reach the bank, and where is cash unapplied?
- How much can a lender advance against these receivables, and is expected loss covered?
- How sensitive is the facility to higher rates and slower collections?

## Methods

- Rule-based matching in SQL, rules stored as data: exact and normalised trace numbers, split
  deposits, lockbox subset sums, payer + amount + date window. A rule accepts only a unique
  candidate; ambiguous items go to an exception queue.
- Control identities per payment and per day; `recon.close_day` refuses to close a day that is one
  cent off.
- Borrowing base: eligibility waterfall (denied, disputed, aged, cross-aged), concentration limit
  as a fixed point, advance rate, credit-balance reserve. Expected loss = PD x LGD x EAD, PD from an
  absorbing Markov chain. Stress at higher SOFR and slower payment.

## Measured results (simulated)

| Result | Value | Label |
|---|---|---|
| Control totals tied to the cent | <!--n:recon.control_checks-->24<!--/n--> checks, <!--n:recon.control_all_passed-->all passed<!--/n--> | synthetic_record |
| Days closed / in calendar | <!--n:recon.days_closed-->820<!--/n--> / <!--n:recon.days_total-->820<!--/n--> | synthetic_record |
| Payments matched to deposits on self-generated defects, precision / recall vs planted truth | <!--n:recon.r2b.precision-->1.0000<!--/n--> / <!--n:recon.r2b.recall-->0.9616<!--/n--> | synthetic_record |
| Lockbox bundles, truncated traces, date shifts handled | <!--n:recon.case.lockbox.payments-->139<!--/n-->, <!--n:recon.case.truncated.payments-->89<!--/n-->, <!--n:recon.case.dateshift.payments-->136<!--/n--> payments, all matched | synthetic_record |
| Unapplied payer credits | <!--n:recon.unapplied.amount-->$2,048,321.11<!--/n--> on <!--n:recon.unapplied.n-->64<!--/n--> items: <!--n:recon.unapplied.true-->12<!--/n--> true deposits without remittance, <!--n:recon.unapplied.manual-->52<!--/n--> queued for manual matching | synthetic_record |
| Payments with no matched deposit | <!--n:recon.remit_no_deposit.n-->51<!--/n-->: <!--n:recon.remit_no_deposit.true-->7<!--/n--> truly missing, <!--n:recon.remit_no_deposit.manual-->44<!--/n--> whose deposit the rules queued | synthetic_record |
| Availability on <!--n:credit.certificate_date-->2025-09-30<!--/n--> | <!--n:credit.availability-->$2,656,726<!--/n--> on <!--n:credit.eligible-->$3,204,908<!--/n--> eligible | model_estimate |
| Advance rate / concentration limit / age limit | <!--n:credit.advance_rate-->85%<!--/n--> / <!--n:credit.concentration_limit-->30%<!--/n--> / <!--n:credit.max_age_days-->120<!--/n--> days | financial_assumption |
| Expected loss vs lender margin | <!--n:credit.expected_loss-->$662,340<!--/n--> vs <!--n:credit.lender_margin-->$480,736<!--/n-->: covered = <!--n:credit.el_covered-->no<!--/n--> | model_estimate |
| Annual interest, base vs +<!--n:credit.shock_large-->200<!--/n--> bp | <!--n:credit.base_interest-->$179,395<!--/n--> vs <!--n:credit.stress_200bp_interest-->$233,268<!--/n--> | model_estimate |

## Questions a reader may ask

1. **Why is bank-side recall <!--n:recon.r2b.recall-->0.9616<!--/n-->, not 1?** Two equal untraced payments fit two deposits
   equally, and a split deposit's second credit can carry no trace. The rules queue these
   (<!--n:recon.case.equal_untraced.queued-->36<!--/n--> and <!--n:recon.case.split_untraced.payments-->8<!--/n--> payments) rather than guess, and the cash identity still ties because they sit in explicit buckets.
2. **What does close_day check?** That the statement balance chains row by row, every deposit and
   payment is fully allocated, every unmatched item is queued and every posted line satisfies the
   identity. Tests corrupt one cent and expect a refusal.
3. **Why does expected loss exceed the margin?** Many eligible claims are still denied later, so the
   NCV-weighted PD is high. The answer is a lower advance rate or tighter eligibility, which is
   what the check is for.
4. **How is the concentration limit computed?** Each payer is capped at L times the final eligible
   base E, so E = sum of min(e_p, L E), solved by iteration.
5. **Is any of this real bank data?** No. All records are simulated; SOFR (<!--n:public.sofr-->3.91%<!--/n-->) is the only real
   rate, and the advance rate and spread are labelled assumptions.
