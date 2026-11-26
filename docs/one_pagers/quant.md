# Receivables Reconciliation & Risk Engine: Quant one-pager

**Summary.** Probability and validation work on top of an exact payment reconciliation: an
absorbing Markov chain of the receivable life cycle, a survival-based Monte Carlo cash forecast
with cash-at-risk, expected loss for a borrowing base, and a bootstrapped chain-ladder reserve.
Every model is fit before a cutoff and tested after it. Healthcare claims are the demonstration
data, simulated for a fictional hospital.

## Business questions answered

- What is the probability that an open receivable is eventually collected, from each state?
- Will collections cover cash needs over 13 weeks, and how bad is the 5% tail?
- How much is still owed on claims already incurred, and with what uncertainty?

## Methods

- Absorbing Markov chain: MLE transition matrix, N = (I - Q)^-1, B = N R, claim-level bootstrap;
  chi-square tests of the Markov property; time-in-state correction.
- Payment-lag survival with right censoring (Kaplan-Meier; lognormal, gamma, Weibull by AIC);
  Monte Carlo of receipts; cash-at-risk and CVaR; weekly-origin coverage backtest.
- Chain-ladder, Mack standard error, ODP bootstrap, Bornhuetter-Ferguson; retrospective test.
- Leakage tests: deleting future rows must not change earlier outputs.

## Measured results (simulated)

| Result | Value | Label |
|---|---|---|
| P(eventually paid) from SUBMITTED, 95% interval | <!--n:markov.p_paid-->90.9%<!--/n--> (<!--n:markov.p_paid_ci-->90.6% to 91.3%<!--/n-->) | model_estimate |
| Largest error of an estimated transition probability vs planted | <!--n:markov.max_abs_error-->0.058<!--/n--> | model_estimate |
| Hold-out, claims in DENIED: plain chain / corrected / observed | <!--n:markov.denied_pred_memoryless-->50.2%<!--/n--> / <!--n:markov.denied_pred_duration-->28.1%<!--/n--> / <!--n:markov.denied_observed-->25.2%<!--/n--> | model_estimate |
| Hold-out Brier: constant / chain / corrected chain | <!--n:markov.brier_constant-->0.1073<!--/n--> / <!--n:markov.brier_memoryless-->0.0855<!--/n--> / <!--n:markov.brier_duration-->0.0799<!--/n--> | model_estimate |
| Cash backtest: weeks inside 80% / 95% band | <!--n:cash.cov80-->79.1%<!--/n--> / <!--n:cash.cov95-->96.1%<!--/n--> of <!--n:cash.n_weeks-->741<!--/n--> | model_estimate |
| Cash-at-risk (95%) at <!--n:cash.question_date-->2025-09-29<!--/n--> | <!--n:cash.car95-->$648,538<!--/n--> | model_estimate |
| Chain-ladder unpaid, bootstrap 90% interval | <!--n:q4.cl_unpaid-->$863,526<!--/n--> (<!--n:q4.boot_p05-->$590,325<!--/n--> to <!--n:q4.boot_p95-->$1,168,996<!--/n-->) | model_estimate |
| Simulator's true unpaid, inside interval | <!--n:q4.true_unpaid-->$663,429<!--/n-->, <!--n:q4.true_inside-->yes<!--/n--> | synthetic_record |

## Questions a reader may ask

1. **Is the life cycle Markov?** No: the step after DENIED depends on the denial reason
   (chi-square <!--n:markov.chi2_after_denied-->2136<!--/n-->) and later reviews are denied more often. The chain is kept as a baseline, plus
   a time-in-state correction, which improved the hold-out Brier score.
2. **Why survival analysis for payment lags?** Unpaid claims are right-censored; dropping them
   biases lags short. Kaplan-Meier keeps them in the risk set.
3. **Are your coverage numbers independent?** No. Overlapping 13-week windows are dependent, so
   the Wilson interval (<!--n:cash.cov80_ci-->76.0% to 81.9%<!--/n-->) is too narrow; it is reported with that caveat.
4. **Why was chain-ladder <!--n:q4.cl_error-->30%<!--/n--> high?** The newest month is multiplied by a development factor of
   <!--n:q4.cdf0-->8.62<!--/n-->, and <!--n:q4.top10_share-->63%<!--/n--> of the true unpaid sits in 10 large claims. Bornhuetter-Ferguson reduces the
   newest-month problem.
5. **What does simulation prove?** Parameter recovery and correct decision logic only, never
   anything about real payers.
