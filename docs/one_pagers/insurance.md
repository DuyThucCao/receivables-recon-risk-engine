# Payment Reconciliation & Receivables Risk Engine: Insurance one-pager

**Summary.** Claims-to-payment engine from a payer-actuary and claims-analytics angle: a paid
development triangle built in SQL that ties to the reconciled ledger, chain-ladder with Mack and
bootstrap uncertainty, frequency-severity checks, and line-level comparison of allowed amounts
with contract rates. All claims and payers are simulated; payer denial rates are calibrated to
ranges computed by this project from the CMS Transparency in Coverage file.

## Business questions answered

- How much is still owed on claims incurred but not yet paid (unpaid claims reserve)?
- Are claims paid at contract, and which payer deviates most?
- How do denial, appeal and payment flows move a claim to its final outcome?

## Methods

- Development triangle `mart.dev_triangle_at(d)` using only payments known on date d.
- Chain-ladder; Mack (1993) standard error; ODP bootstrap (England and Verrall 2002);
  Bornhuetter-Ferguson with a Cape Cod ratio; retrospective re-estimation at four past dates.
- Frequency: Poisson GLM with over-dispersion tests. Severity: gamma vs lognormal by AIC.
- Allowed vs contract comparison per claim line; bootstrap resampling whole claims.

## Measured results (simulated)

| Result | Value | Label |
|---|---|---|
| Triangle paid to date, difference to reconciled paid | <!--n:q4.triangle_paid-->$58,550,706.63<!--/n-->, <!--n:q4.triangle_tie-->$0.00<!--/n--> | synthetic_record |
| Chain-ladder unpaid, Mack s.e. | <!--n:q4.cl_unpaid-->$863,526<!--/n-->, <!--n:q4.mack_se-->$208,351<!--/n--> | model_estimate |
| Bootstrap 90% interval | <!--n:q4.boot_p05-->$590,325<!--/n--> to <!--n:q4.boot_p95-->$1,168,996<!--/n--> | model_estimate |
| Bornhuetter-Ferguson unpaid | <!--n:q4.bf_unpaid-->$827,454<!--/n--> | model_estimate |
| Simulator's true unpaid (evaluation only) | <!--n:q4.true_unpaid-->$663,429<!--/n--> | synthetic_record |
| Retrospective dates inside interval | <!--n:q4.retro_inside-->4 of 4<!--/n--> | model_estimate |
| Claim-count dispersion (1 = Poisson) | <!--n:q4.poisson_dispersion-->0.87<!--/n--> | model_estimate |
| Highest payer shortfall rate vs contract | <!--n:q7.top_payer-->P03<!--/n-->: <!--n:q7.top_rate-->1.61%<!--/n--> (<!--n:q7.top_rate_ci-->1.15% to 2.15%<!--/n-->) | model_estimate |
| Simulated payers' first-pass denial rates, set at CMS percentiles | <!--n:public.denial_rate_min-->11.1%<!--/n--> to <!--n:public.denial_rate_max-->23.3%<!--/n--> | financial_assumption |
| CMS issuer in-network denial rates, p10 to p90 (<!--n:public.cms_year-->2025<!--/n-->, computed by this project from CMS data) | <!--n:public.cms_p10-->9.0%<!--/n--> to <!--n:public.cms_p90-->24.5%<!--/n--> | observed_public |

## Questions a reader may ask

1. **Chain-ladder assumptions?** Stable development pattern, factors independent of period size, no
   tail beyond the data. Broken by the newest month (CDF <!--n:q4.cdf0-->8.62<!--/n-->), changing payment speed and
   lumpy large claims.
2. **Why add Bornhuetter-Ferguson?** It leans on exposure for immature periods, which narrowed the
   retrospective error range.
3. **Is this IBNR?** Mostly incurred-but-not-paid on reported claims. Claims not yet submitted by
   the valuation date are missing from the exposure, which is why both methods underestimate at
   month-end retro dates.
4. **Why compare allowed, not paid, with contract?** Deductibles and coinsurance are part of the
   allowed amount, so paid comparisons would count patient share as underpayment.
5. **Is the CMS denial rate the hospital's denial rate?** No. It covers marketplace issuers only,
   counts all denial reasons and is self-reported. It sets a plausible range, nothing more.
