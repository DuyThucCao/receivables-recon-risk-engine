# Payment Reconciliation & Receivables Risk Engine: Consulting one-pager

**Summary.** An economic-consulting style analysis on reconciled data: which payers underpay
against contract, by how much, what payment delay is worth, and why a payer would do either. It
ends in a two-page memo with recommendations (`docs/consulting_memo.md`). Payers, claims and
payments are simulated for a fictional hospital; SOFR is the only real series.

## Business questions answered

- Which payers underpay against contract, by how much, with what confidence?
- What is the value of payment float, and how much follows denials?
- When should the hospital appeal, and what contract terms change the payer's incentives?

## Methods

- Allowed vs contract per claim line (patient share excluded), lines classified into priced below
  contract, reduced after payment, pending, overpaid, at contract.
- Percentile bootstrap resampling whole claims (<!--n:q7.bootstrap_reps-->2,000<!--/n--> replicates per payer).
- Float = paid x SOFR x days / 365; late = beyond an assumed <!--n:q7.prompt_pay_days-->30<!--/n-->-day window.
- Appeal break-even s* = c / P(win); deterrence penalty pi >= 1/(q w) - 1; repeated game with a
  renewal discount factor.

## Measured results (simulated)

| Result | Value | Label |
|---|---|---|
| Total shortfall at first pricing | <!--n:q7.shortfall-->$334,486.29<!--/n--> | model_estimate |
| Highest shortfall rate | <!--n:q7.top_payer-->P03<!--/n--> <!--n:q7.top_rate-->1.61%<!--/n--> (<!--n:q7.top_rate_ci-->1.15% to 2.15%<!--/n-->), <!--n:q7.p03_share-->13.9%<!--/n--> of claims | model_estimate |
| Ranking by shortfall rate | <!--n:q7.ranking-->P03, P06, P05, P02, P04, P01<!--/n--> | model_estimate |
| Line flags on self-generated defects (vs planted truth): precision / recall | <!--n:q7.flag_precision-->0.9988<!--/n--> / <!--n:q7.flag_recall-->1.0000<!--/n--> | model_estimate |
| Planted rate inside 95% interval | <!--n:q7.planted_share_inside-->5<!--/n--> of 6 payers | model_estimate |
| Float total / beyond the window | <!--n:q7.float_total-->$363,534.00<!--/n--> / <!--n:q7.float_late-->$179,197.70<!--/n--> | model_estimate |
| Appeal thresholds | <!--n:q7.threshold_min-->$93.23<!--/n--> to <!--n:q7.threshold_max-->$126.40<!--/n--> per claim | model_estimate |
| Expected net recovery above threshold | <!--n:q7.net_recovery-->$104,252.32<!--/n--> | model_estimate |
| Penalty multiple needed to deter underpricing | <!--n:q7.penalty_min-->1.07<!--/n--> to <!--n:q7.penalty_max-->1.81<!--/n--> | model_estimate |
| Appeal cost | <!--n:q7.appeal_cost-->$45<!--/n--> | financial_assumption |

## Questions a reader may ask

1. **Is the payer acting strategically?** The data cannot say: simulated underpayments are random
   draws. The model says what a rational payer could do; the real-data test is bunching of
   shortfalls just below the appeal threshold.
2. **Why resample whole claims?** Lines of one claim share one pricing decision, so line-level
   resampling understates uncertainty.
3. **Why does recovery not deter?** If losing an appeal only means paying what was owed, the
   payer keeps the shortfall whenever the hospital does not appeal or loses; only a penalty above
   1/(q w) - 1 times the shortfall flips the sign.
4. **Why do interest clauses work for delay but not underpricing?** Delay's gain is interest on
   held cash, which a clause above the payer's cost of funds removes; underpricing's gain is the
   whole shortfall.
5. **Why only <!--n:q7.top_payer-->P03<!--/n--> with confidence?** Its interval does not overlap the others'; the rest overlap,
   so their order is not reliable.
