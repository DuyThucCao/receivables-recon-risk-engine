# Model notes: claim life-cycle chain, 13-week cash forecast, borrowing base

Area: chain, cash and credit (DESIGN.md section 9). Modules `src/recon_engine/models/markov.py`,
`cashflow.py`, `credit.py`; SQL `sql/models/40_chain.sql`, `41_cash_forecast.sql`,
`42_borrowing_base.sql`; tests `tests/test_markov.py`, `test_cashflow.py`, `test_credit.py`.

```
export RRE_PG_DIR=~/.cache/receivables-engine/pg-risk
python run_all.py --skip-generate     # build the database
python scripts/run_models_risk.py     # results/{markov,cashflow,credit}.json, *_validation.json, figures
python -m pytest tests/test_markov.py tests/test_cashflow.py tests/test_credit.py
```

**Read this first.** All data are simulated for a fictional hospital and six fictional payers.
The numbers below show two things only: (a) how well each method recovers parameters planted in
the generator and (b) whether the decision logic behaves correctly. They are not findings about
real payers, banks, lenders or interest-rate markets. Every number is quoted from `results/*.json`
(run of 2026-11-22). Labels follow DESIGN.md section 8.

The simulated hospital bills services up to 2025-12-31 and the snapshot is 2026-03-31. At the
snapshot the book is in run-off (three months with almost no new claims), so the Treasurer and
lender questions are also answered at a date while billing is still running (2025-09-29/30),
where the later data let us check the answer.

---

## 1. Shared building block: absorbing Markov chain of the claim life cycle

**Question.** For a claim in a given status today, what is the probability it ends PAID rather
than WRITTEN_OFF, and how many more status changes will it go through? (Inputs to Q3, Q4, Q5.)

**Data.** `core.fact_claim_event` (life-cycle feed), `core.fact_claim_line` (payer, submission
date). Point-in-time functions `mart.chain_events_asof(cutoff)` and `mart.chain_claim_asof(cutoff)`
read only rows dated on or before the cutoff. Marts: `mart.chain_transition_count`
(synthetic_record), `mart.chain_matrix`, `mart.chain_absorption` (model_estimate).

**Method.**
- States: transient SUBMITTED, IN_REVIEW, PENDED, DENIED, APPEALED; absorbing PAID, WRITTEN_OFF.
  The model is the embedded jump chain (which status comes next), not time.
- Counts `n_ij` of consecutive statuses per claim; multinomial MLE per row `P_ij = n_ij / n_i`,
  by payer. A payer row with fewer than 50 observed exits uses the pooled row (flagged).
- Censoring: exits are counted only for visits that began at least 180 days before the cutoff
  (`maturity_days`), because fast exits are over-represented among recent entries.
- Canonical form `P = [[Q, R], [0, I]]`, fundamental matrix `N = (I - Q)^-1` (expected visits),
  absorption probabilities `B = N R`, expected transitions to absorption `t = N 1`, variance
  `(2N - I) t - t*t`.
- Claim-level bootstrap (500 resamples via multinomial weights) for a 95% interval of
  P(PAID | SUBMITTED).
- Time-in-state correction (semi-Markov first step): for a claim already `d` days in status `s`,
  `P(next = j | s, d)` is estimated from past visits to `s` that lasted longer than `d`, then
  `p = sum_j P(j | s, d) B[j, PAID]`. Used by cashflow and credit for open claims.
- Optional dollar weighting (`claim_weights=billed`): each transition counts with its claim's
  billed amount, giving the share of *dollars* that end PAID (used by credit for PD).

**Validation** (`results/markov_validation.json`, `results/markov.json`).
- Recovery of the planted visit-weighted matrices at the as-of date: max absolute cell error
  **0.0576** over 6 payers x 35 cells (P04 APPEALED->PAID); max standardised error
  **2.91** standard errors (tolerance 4). Without the maturity filter: 0.0722 and 3.24, so the
  filter helps a little.
- P(PAID | SUBMITTED): largest absolute error against the planted mean over claims **0.0071**;
  the planted value lies inside the 95% bootstrap interval for **6 of 6** payers
  (e.g. P03 estimate 0.857, planted 0.8635, interval [0.8455, 0.8689]).
- Out-of-sample calibration (fit at 2025-06-30, outcomes observed up to 2026-03-31, no ground
  truth used): for 2,074 claims open at the cutoff, the memoryless chain predicts P(PAID) 0.8175
  against 0.7797 observed (z = -4.92); for DENIED claims 0.5024 against 0.2519 (z = -9.95).
  With the time-in-state correction: 0.7823 vs 0.7797 (z = -0.38) and 0.2809 vs 0.2519
  (z = -1.41). Brier score 0.0855 (memoryless), 0.0799 (corrected), 0.1073 (constant rate).
  98 hold-out claims were still open at the evaluation date and are excluded, which biases the
  observed rate towards fast outcomes (claims submitted after the cutoff: predicted 0.909,
  observed 0.9202, z = 2.71: a weak spot, probably this censoring plus claim heterogeneity).

**Where the data violate the Markov assumption** (`markov_assumption_diagnostics`).
1. What follows DENIED depends on the denial reason: chi-square 2136 (dof 15, p ~ 0).
2. Appeal success depends on the reason of the appealed denial: P(PAID) from 0.17
   (non-covered) to 0.578 (administrative), p = 2.2e-15.
3. Heterogeneity: a resubmitted claim is denied more often on later reviews (0.197) than on the
   first (0.157), p = 9e-10.
4. Duration dependence: DENIED claims that have waited long are mostly heading for write-off
   (fixed by the time-in-state correction above).
The pooled chain still estimates the population-average matrix consistently; individual
predictions inherit these effects.

**Limitations.** Status-only state space (no reason, service line or amount); the chain ignores
time (handled in cashflow); the bootstrap treats claims as independent; the planted comparison
is to the generator's visit-weighted matrix, which is exactly what the count MLE estimates.

**Interview (5 sentences).** I modelled each claim's status history as an absorbing Markov chain
with PAID and WRITTEN_OFF as absorbing states and estimated the transition matrix by counting
transitions per payer, which is the maximum-likelihood estimate. From the matrix I computed the
fundamental matrix N = (I - Q)^-1, whose entries are expected visits, and B = N R, the
probability of ending paid or written off from each status. On simulated data the estimates
recover the planted matrices within 0.06 per cell and the planted paid probability lies inside
the bootstrap interval for all six payers. An out-of-sample check showed the memoryless model
badly overstates recovery for claims that have sat in DENIED for a while, so I added a
time-in-state correction that uses only past visits at least that long. I also tested and
reported where the Markov assumption fails: denial reason and repeat reviews carry memory.

---

## 2. Question 3 (Treasurer): will we be short of cash in the next 13 weeks?

**Data.** Claims and states at the forecast date (`mart.chain_claim_asof`), remittance postings
up to that date, reconciled payment-to-deposit matches for deposit lags
(`mart.cash_deposit_lag_asof`, several deposit rows per payment are allowed), remitted but not
yet deposited payments (`mart.cash_in_transit_asof`), bank balance (`mart.cash_opening_balance`),
payer deposits for actuals (`mart.cash_payer_receipts`). Marts: `mart.cash_forecast`,
`mart.cash_forecast_backtest`, `mart.cash_forecast_lag` (model_estimate).

**Method.**
1. Payment lag T = days from submission to the payer's payment run (PAID event). Open claims are
   right-censored. Each open claim enters the risk set with weight
   P(eventually PAID | status, time in status) from the chain; written-off claims get 0; weights
   are multiplied by dollars at stake, so the curve is the timing of *dollars*.
   Weighted Kaplan-Meier: `S(t) = prod (1 - d_j / n_j)`.
2. Log-normal, gamma and Weibull fitted by weighted censored maximum likelihood
   (`sum w [d log f(t) + (1-d) log S(t)]`), compared by AIC and by distance to KM. Simulation
   curve: KM where at least 30 claims are at risk, best parametric tail beyond.
3. Deposit lag: empirical distribution per payer.
4. Monte Carlo (2,000 paths): open claims paid with probability p_i at a time drawn from S given
   T > age, amount = billed x paid-to-billed ratio resampled within the payer's billed-amount
   decile (dollar-weighted); new claims as Poisson arrivals at the trailing 91-day rate, uniform
   arrival days, amount resampled from recent paid claims; a bootstrap level factor per path
   carries uncertainty in the average claim value; in-transit remittances in week 1.
5. Shortfall: position_k = opening balance + cumulative receipts - k x weekly need; shortfall =
   max(0, minimum balance - min_k position_k). Weekly need = `cash_need_factor` (0.95,
   financial_assumption, config/calibration.yaml) x median weekly receipts of the trailing 26
   weeks; minimum balance $1,000,000 (financial_assumption). Reported: P(shortfall), VaR95 and
   CVaR95 of the shortfall, cash-at-risk (mean minus 5th percentile of 13-week receipts), and the
   curve of P(shortfall) and CVaR95 for factors 0.70-1.20.
6. Backtest: 57 weekly forecast origins from 2024-09-02 to 2025-09-29, each using only data
   dated on or before its origin.

**Validation** (`results/cashflow.json`, `results/cashflow_validation.json`).
- Lag recovery: weighted-KM quartiles of the first-pass lag (no denial, pends allowed) match
  the planted realised quartiles within **0 days** at the as-of cutoff and **2 days** at
  2025-06-30 for all six payers. Log-normal had the lowest AIC for every payer (P01: 24,833 vs
  gamma 25,533, Weibull 25,778), but its largest gap to KM is still 0.05-0.10, which is why the
  simulation uses KM for the body.
- Backtest coverage (hybrid curve; Wilson 95% intervals in brackets):

  | interval | target | weekly, 741 origin-weeks | week 1 only, 57 distinct weeks | 13-week total, 57 origins | 13-week total, 5 non-overlapping |
  |---|---|---|---|---|---|
  | 80% | 0.80 | 0.791 [0.760, 0.819] | 0.754 [0.629, 0.848] | 0.877 [0.768, 0.939] | 1.0 [0.566, 1.0] |
  | 95% | 0.95 | 0.961 [0.944, 0.973] | 0.947 [0.856, 0.982] | 0.912 [0.811, 0.962] | 1.0 [0.566, 1.0] |

  Weekly and overlapping 13-week points are dependent, so their Wilson intervals are too narrow;
  the week-1 and non-overlapping rows use distinct periods but are small. Coverage is consistent
  with nominal; with only 5 independent 13-week windows the total-coverage evidence is weak.
  Weekly MAPE 0.185, 13-week MAPE 0.034, mean 13-week bias -0.5%. The pure parametric curve is
  similar (weekly 80%: 0.796, 95%: 0.955).
- Two earlier versions failed the backtest and were fixed: a count-weighted lag curve and mean
  paid-to-billed ratios over-forecast receipts by more than half and then by 10-20%, because
  large claims are denied more often (so paid later) and have lower paid-to-billed ratios.
- Treasurer's answer at 2025-09-29 (billing still running): opening balance $2,868,722; weekly
  need $605,758 (0.95 x median $637,640); expected 13-week receipts $8,194,148, 5th percentile
  $7,545,610, cash-at-risk $648,538. **P(shortfall) = 0.0, CVaR95 = $0** at the base case;
  the curve turns at higher needs: factor 1.10 -> P = 0.007; 1.15 -> P = 0.0915, CVaR95
  $258,925; 1.20 -> P = 0.408, CVaR95 $672,716. Actual receipts were $8,263,513 (58th
  percentile of the forecast) and no shortfall occurred at the assumed need.
- At the as-of date (run-off): P(shortfall) = 1.0, CVaR95 $4,060,032 because receipts fall to
  a forecast $789,726 over 13 weeks. This is the simulation stopping billing, not a finding.

**Limitations.** The weekly need is an assumption anchored to receipts (the simulated statement
has no payroll); the collection account sweeps excess to a concentration account, so its
balance is not total liquidity; arrivals are Poisson at a trailing rate (no seasonality, no
trend); the lag curve is per payer only; post-payment adjustments and unapplied cash are not
modelled; parameter uncertainty is only partly propagated.

**Interview (5 sentences).** I estimated how long payers take to pay with a Kaplan-Meier curve
that treats still-open claims as censored and weights each by its Markov probability of ever
being paid, and I weighted by dollars because big claims pay later. I then simulated 13 weeks
of receipts: open claims pay according to the curve conditional on their age, new claims arrive
as a Poisson process, and amounts are resampled from recent paid claims. Against a stated weekly
cash need I report the probability of breaching a minimum balance, the CVaR of the shortfall and
a curve across need assumptions. I backtested at 57 weekly origins using only data available at
each date, and the 80% and 95% weekly intervals covered 79% and 96% of actual weeks. The first
backtests failed, which is how I found the size effect, and I report that the 13-week coverage
rests on only five independent windows.

---

## 3. Question 5 (Bank credit analyst): how much can we lend against these receivables?

**Data.** `mart.bb_lines_asof(d)`: every claim line submitted by d with postings up to d and
the claim's status at d. Marts: `mart.borrowing_base`, `mart.borrowing_base_line`,
`mart.borrowing_base_stress` (model_estimate). Assumptions: advance rate 0.85, age limit 120
days, concentration limit 0.30, cross-age 0.50, spread 2.75% (all financial_assumption,
config/calibration.yaml); SOFR 3.91% as of 2026-11-19 (observed_public, FRED).

**Method.**
1. Tie-out: at the as-of date the point-in-time open balance equals `recon.claim_line_balance`.
2. Net collectible value (NCV) per open line: billed x payer collection ratio (paid / billed of
   claims paid in the previous year) if unadjudicated; `max(0, min(open, billed x ratio - paid))`
   after a partial payment; denied lines valued as if overturned (they are excluded anyway).
3. Eligibility waterfall (first failing rule): DENIED; DISPUTED (APPEALED or PENDED); AGED
   (> 120 days since submission); CROSS_AGED (payer with > 50% of NCV aged).
4. Concentration: final base E solves E = sum_p min(e_p, 0.30 E) (iteration to the largest
   fixed point; with fewer than 1/0.30 payers the base collapses to 0, as the rule says).
5. Availability = 0.85 x eligible - credit balances owed back to payers.
6. Expected loss per payer = PD x LGD x EAD. PD = NCV-weighted write-off probability from the
   dollar-weighted chain with the time-in-state correction; LGD = 1 - recovery on claims written
   off in the prior year; EAD = eligible NCV. Check: EL <= lender margin (1 - advance) x eligible.
7. Stress (ACT/360): annual interest on the drawn facility and the financing cost of collection
   delay, `sum NCV x P(paid) x rate x E[remaining days | age] / 360`, with remaining days from the
   cashflow lag curve; scenarios base, +100 bp, +200 bp, 25% slower payment (also ages the book
   x 1.25 and re-runs eligibility), and +200 bp with slower payment.

**Validation and results** (`results/credit.json`, `results/credit_validation.json`).
- Tie-out at 2026-03-31: 72,256 lines, open total $2,837,491.10 both ways, **0 lines differ**.
- Certificate 2025-09-30: gross NCV $7,034,465; ineligible denied $2,574,902, disputed $767,428,
  aged $487,227; eligible $3,204,908 (no concentration excess); credit balances $67,446;
  **availability $2,656,726**. A 90-day age limit gives $2,613,701.
- Expected loss $662,340 against a lender margin of $480,736: **EL is not covered by the margin**,
  so the decision rule flags the 85% advance rate as too high for this book. LGD is 1.0 for every
  payer: written-off claims in this simulation recover nothing.
- **Expected-loss backtest** (eligible pool of 2025-09-30 followed to 2026-03-31): predicted EL
  **$662,340**, realised loss on lines that were written off **$625,007**; predicted NCV-weighted
  PD 0.2067 vs realised write-off share 0.195 (61 lines still open). The count-weighted chain
  predicted only 0.0814: the first version understated losses by more than half because large
  claims are denied more often. Against the generator's ultimate outcomes, NCV-weighted PD 0.2067
  vs 0.2163 ultimate. The dollar-weighted PD is a portfolio figure; per line it overstates the
  write-off probability of small claims (count-level ultimate rate 0.0778 vs 0.2065 predicted).
- Collections: 77.1% of eligible NCV was collected within six months.
- Stress at 2025-09-30: rate 6.66% base; annual interest $179,395 (+100 bp: $206,332;
  +200 bp: $233,268); financing cost of collection delay $42,986 (+200 bp: $55,894; slower:
  $53,732; +200 bp and slower: $69,868); slower payment cuts availability to $2,628,022.
- At the as-of date (run-off) every remaining line is denied, disputed or aged, so eligible = 0
  and availability = -$95,639 (credit balances): the rules work, the book is simply old.

**Limitations.** Government-payer anti-assignment rules, dilution reserves and lender-specific
eligibility criteria are not modelled; NCV uses a payer-level collection ratio; LGD = 1 is a
property of the simulation; the facility is assumed fully drawn; the spread and advance rate
are assumptions, not market quotes.

**Interview (5 sentences).** I built a borrowing-base certificate the way an asset-based lender
does: value each open claim at its expected collectible amount, remove denied, disputed and aged
receivables, apply a cross-age rule and a 30% payer concentration cap, then advance 85% of what
is left. The point-in-time balances tie to the reconciliation to the cent, and nothing dated
after the certificate date can change it, which a test proves. Expected loss is PD x LGD x EAD
with PD from the Markov chain weighted by dollars, and on a past certificate it predicted
$662k against $625k realised. The first, count-weighted version predicted less than half of
that, which taught me that the loss is concentrated in large claims. The stress test reprices
the facility at +100 and +200 basis points over SOFR and slows payments by 25%, showing both the
interest cost and the smaller borrowing base.
