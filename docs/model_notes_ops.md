# Model notes: operations and reserves (Q2, Q4)

All data are **simulated** for a fictional hospital and six fictional payers. The numbers below show
two things only: whether each method recovers what was planted in the generator, and whether the
decision rule behaves correctly. They are not findings about real payers. Every number is quoted
from `results/prioritize.json`, `results/prioritize_evaluation.json`, `results/reserve.json` or
`results/reserve_evaluation.json` (as-of date 2026-03-31, seed 20260331). Labels follow
`docs/DESIGN.md` section 8.

How to reproduce:

```
export RRE_PG_DIR=~/.cache/receivables-engine/pg-ops
python run_all.py --skip-generate     # build the database
python scripts/run_models_ops.py      # run both models, then evaluate
python -m pytest tests/test_prioritize.py tests/test_reserve.py
```

The model modules read only the database and `cfg`. Only `scripts/run_models_ops.py` opens
`data/truth/`, and it writes what it finds to the `*_evaluation.json` files.

---

## Q2. Which denied claims should staff work first today? (`models/prioritize.py`)

**Who asks:** the revenue-cycle lead. **Output:** `mart.work_queue` (today's ranked list with a
plain-English reason on every row), `mart.work_queue_coef`, `mart.work_queue_backtest`,
`mart.work_queue_backtest_selection`. SQL: `sql/models/55_work_queue.sql`. Figures:
`figures/prioritize_reliability.png`, `prioritize_backtest.png`, `prioritize_coefficients.png`.

### Data

`mart.denial_episodes(d)` returns one row for each DENIED event in the claim status feed, seen as
of date `d`. Events and remittance postings dated after `d` are invisible to it, and a test checks
this by deleting the future and confirming the output does not change. For every denial it records
what happened next (resubmitted, appealed, written off, or nothing yet). For an appeal it records
the outcome: OVERTURNED if the next event is PAID, UPHELD if the next event is DENIED again.
Money comes from the reconciliation core. `contract_allowed` is the sum of
`recon.claim_line_balance.expected_allowed` over the claim's lines. Provider-level (PLB) remittance
rows are excluded.

- Training set: 1,079 appeals decided on or before the cutoff 2025-07-01, with an overturn rate of 0.4319 (`synthetic_record`).
- Test set: 442 appeals on denials dated on or after the cutoff whose outcome was known by the as-of date, with an overturn rate of 0.4186.
- Held-out denials for the backtest: 1,980, which is 10.1 per business day.

### Method

1. **Overturn model (logistic regression, statsmodels GLM with a binomial family and logit link).**

   `logit P(overturn) = b0 + b_payer + b_reason + b_service_line + b_amt (ln charge - mean) + b_age (months from submission to denial)`

   Categorical variables use treatment coding. The reference level of each is the most common level in
   the training data, chosen from the data and not from the generator. Each coefficient is a log odds
   ratio against that reference level.
2. **Baselines:** (a) the training overturn rate for every appeal; (b) the training overturn rate of
   each reason category, a lookup table of the kind a denial team might keep.
3. **Value of working claim i** (all assumptions are labelled `financial_assumption`):

   `V_i = p_i x R_i - c_i`, and `V_i = 0` once the appeal window has closed

   - `R_i = contract_allowed_i x rho_payer`. Here `rho_payer` is the share of contract allowance the payer
     actually paid on clean lines before the cutoff. It nets out contractual adjustment, patient
     responsibility and typical underpayment.
   - `c_i = minutes_i x $45 / 35 min`, an implied loaded cost of $77.14 per hour, taken from
     `config/calibration.yaml`.
   - `minutes_i = 35 x effort multiplier(reason)`. The multipliers run from 0.6 for administrative denials to 1.6 for
     medical necessity. They are an illustrative, labelled assumption. A uniform-effort sensitivity is reported.
   - The appeal window is 90 days from the denial date. This is an assumption; for context, Medicare's
     redetermination deadline is 120 days under 42 CFR 405.942.
4. **Today's list: an exact 0/1 knapsack.** Maximise `sum V_i x_i` subject to `sum minutes_i x_i <= 24 h x 60`,
   with each `x_i` either 0 or 1. It is solved by dynamic programming over integer minutes, in O(n x capacity)
   time. When every claim takes the same number of minutes, the knapsack reduces to sorting by value and
   taking the top k, which is then provably optimal. Claims with `V_i <= 0` are never chosen.
5. **Backtest (daily replay).** On every weekday from the cutoff to the as-of date, each policy picks
   claims that are inside the appeal window, under a daily budget of hours. A claim that has been
   worked leaves the pool. The policies are the knapsack, oldest first and largest billed first. The
   two rules fill the day in their own order and skip claims past the window; they do not screen out
   claims of negative value.

### Validation (held-out appeals, `results/prioritize.json` -> `validation`)

| Test set (n = 442) | Brier | Log loss | AUC | Calibration slope (95% CI) |
|---|---:|---:|---:|---:|
| Logistic model | 0.2317 | 0.6554 | 0.6306 | 0.84 (0.47 to 1.20) |
| Baseline: overall rate | 0.2435 | 0.6802 | 0.5000 | n/a |
| Baseline: reason-category rate | 0.2317 | 0.6551 | 0.6259 | 1.06 (0.59 to 1.54) |
| Planted probabilities (the ceiling for any model; evaluation file) | 0.2283 | 0.6476 | 0.6441 | 0.95 |

- **Weak result, reported as weak.** The logistic model beats the overall rate, but on held-out data
  it is no better than the reason-category lookup table: Brier is 0.2317 for both. Appeal
  outcomes are noisy by construction. Even the generator's own probabilities reach an AUC of only 0.644, so
  there is little discrimination left to find.
- **Parameter recovery** (`prioritize_evaluation.json`): all 19 of 19 fitted contrasts have 95% CIs that
  cover the planted values. The correlation between estimates and planted values is 0.914, and the mean absolute error is 0.147
  log-odds (fit at the cutoff). The age coefficient, planted at 0, is estimated at -0.026. The amount
  slope is -0.103 against -0.15 planted, and non-covered denials are -1.39 against -1.2 planted. The model is right,
  but 1,079 appeals is a small sample for 19 parameters.
- **Recoverable amount check:** on 185 overturned held-out appeals, realised payment divided by predicted
  recoverable amount is 0.941, so `R_i` is about 6% too high.
- **Calibration:** the mean prediction is 0.431 against an observed 0.419, and the calibration slope is 0.84. That is mild
  overfitting, but the CI includes 1.

### Decision quality

**Today's list** (as of 2026-03-31, `todays_queue`): 151 claims have DENIED as their latest status with
no action yet. Of these, 10 are past the appeal window and 8 have non-positive value. The knapsack picks 48 claims,
using 1,439 of the 1,440 minutes, for an expected net value of **$324,591**. With the same budget, oldest-first gets $30,948
and largest-first gets $311,468. With uniform effort it picks 41 claims for $315,626.

**Backtest, net dollars over the held-out period** (`backtest`; the full grid is in the figure):

| Hours per day | Knapsack (model p) | Oldest first | Largest first | Same choices valued with planted p: knapsack / largest / oldest | Realised on appealed claims: knapsack / largest / oldest |
|---:|---:|---:|---:|---|---|
| 1 | 3,727,609 | 813,221 | 3,579,899 | 3,693,432 / 3,535,256 / 832,863 | 1,203,967 / 1,182,127 / 779,388 |
| 2 | 4,643,874 | 1,823,491 | 4,505,018 | 4,632,262 / 4,497,665 / 1,857,133 | 1,242,540 / 1,242,580 / 1,242,329 |
| 6 or more | 5,038,759 | 5,035,907 | 5,035,907 | 5,027,853 / 5,025,318 / 5,025,318 | 1,242,939 / 1,242,329 / 1,242,329 |

- The model-p column is circular, because the knapsack maximises exactly that quantity. The planted-p and
  realised columns are not circular.
- A knapsack that knows the planted probabilities reaches $3,705,282 at 1 hour per day. The fitted model reaches $3,693,432,
  which is 99.7% of that oracle.
- **What the result really says:** value is dominated by the dollar amount. Largest-first comes
  within 3 to 5% of the knapsack, and the model's probabilities add the rest. Oldest-first loses
  heavily whenever hours are scarce. Above about 6 hours per day the budget no longer binds, because inflow is
  10.1 denials a day at roughly 35 to 40 minutes each, and all three rules give the same result. The
  configured 24 hours do not bind in the replay. They do bind on today's backlog list.
- On the appealed-claims pool, realised dollars track the expected ranking at 0.75 to 1.5 hours per day. At 2 hours per day or more
  the pool, about 2.3 appeals a day, is fully worked by every rule.

### Limitations

- The overturn outcome is observed only for claims the hospital chose to appeal. In the generator
  that choice depends only on the reason category, so within each reason the outcomes are not selected
  on value. Real data would need a selection model.
- The value is myopic. A claim with 60 days left in its window competes on equal terms with one that has 1 day left;
  there is no option value of waiting.
- The backtest assumes an outcome does not depend on the day the appeal is filed.
- Effort multipliers, the appeal window, cost and staff hours are assumptions. `R_i` overstates realised
  payment by about 6%.
- One appeal per denial episode. Resubmission, the alternative action, is not modelled.

### Interview explanation (5 sentences)

I trained a logistic regression on past appeals to estimate the chance that each denied claim is
overturned, using payer, denial reason, service line, claim size and age, and only data available
before a cutoff date. On later appeals it was well calibrated but only about as good as a simple
table of overturn rates by reason, which is honest. The generator's true probabilities only reach
an AUC of 0.64, so appeal outcomes are mostly luck. I turned each probability into dollars, as probability
times the contract amount the payer would actually pay minus the staff cost, set claims past the appeal window to zero,
and chose today's list with an exact knapsack because appeals take different amounts of time. In a
day-by-day replay with tight staff hours this beat "oldest first" by about $2.9 million and
"largest first" by about $150 thousand over nine months. Once staff hours exceed the daily inflow, though, every rule
gives the same answer, so the ranking only matters when there is a backlog.

---

## Q4. How much is still owed on claims incurred but not yet paid? (`models/reserve.py`)

**Who asks:** the payer actuary. **Output:** `mart.dev_triangle`, `mart.reserve_factors`,
`mart.reserve_by_period`, `mart.reserve_retro`. SQL: `sql/models/50_reserve_triangle.sql`.
Figures: `figures/reserve_development.png`, `reserve_bootstrap.png`, `reserve_retro.png`,
`reserve_factor_stability.png`, `reserve_severity.png`, `reserve_validation.png` (made by the evaluation script).

### Data

`mart.dev_triangle_at(d)` builds the paid triangle as it could have been built on date `d`. It
uses claim lines submitted by `d` and remittance postings dated by `d`, net of reversals and
recoupments, and excludes PLB rows. Rows are accident (service) months, 2024-01 to 2025-12. Columns are
development lags in months, where lag = payment month minus service month. Window functions build the
cumulative columns. At the as-of date the triangle total of $58,550,706.63 ties to
`recon.claim_line_balance.paid` with a difference of 0.00.

### Method

1. **Chain-ladder.** `f_j = sum_i C[i,j+1] / sum_i C[i,j]`, summed over accident months observed at both lags.
   `CDF_j = prod_{k>=j} f_k`, `Ult_i = C[i,latest] x CDF_latest`, and `Unpaid_i = Ult_i - C[i,latest]`.
   No tail factor is applied (assumption): the oldest accident months show no payments after lag 18. Estimated factors are
   f0 = 5.05, f1 = 1.353 and f2 = 1.106; CDF at lag 3 = 1.141 and at lag 0 = 8.62.
2. **Mack (1993) standard errors.** `sigma_j^2` is estimated from the spread of individual ratios, with Mack's rule
   for the last lag. The code reproduces the published Taylor-Ashe benchmark: reserve 18,680,856 and Mack
   s.e. 2,447,095 (unit test).
3. **ODP bootstrap (England and Verrall 2002).** Pearson residuals `(C - m) / sqrt(|m|)` on the
   incremental triangle are scaled by `sqrt(n / (n - p))`, with n = 372 cells and p = 50 parameters. They are resampled into pseudo
   triangles, the chain-ladder is refitted to each, and future cells are drawn from a gamma distribution with mean `|m|` and variance `phi |m|`,
   where phi = 22,383. There are 2,000 replications.
4. **Bornhuetter-Ferguson with a Cape Cod ratio** (added after the retrospective test exposed the
   weakness of chain-ladder in the newest month). `ELR = sum paid / sum(exposure / CDF)`, where exposure =
   contract allowed on reported claims, and `Unpaid_i = ELR x exposure_i x (1 - 1/CDF_i)`. ELR = 0.695.
5. **Frequency-severity view.**
   - Frequency: claim counts per payer and service month (144 cells), fitted with a Poisson GLM that has payer fixed effects
     and an offset of log business days. Over-dispersion is checked three ways: the Pearson
     statistic, the Cameron-Trivedi regression, and a negative binomial (NB2) likelihood-ratio test whose p-value is halved because the null sits on the boundary.
   - Severity: net paid per paid claim, by lag of first payment, fitted as a gamma GLM with log link and as a lognormal.
   - FS unpaid = chain-ladder future first-paid claim counts x mean severity of claims first paid at that lag.
6. **Retrospective test.** Re-estimate at 2024-09-30, 2024-12-31, 2025-03-31 and 2025-06-30, using
   only data known then. Compare with what was actually paid afterwards (observed) and with the projected
   next 3 and 6 months of cash.

### Validation

**Against the simulator's true unpaid amount** (`reserve_evaluation.json`). True unpaid is $663,429, defined as ultimate paid
minus paid by the as-of date.

| Estimate | Unpaid | Error vs true |
|---|---:|---:|
| Chain-ladder | 863,526 | +30.2% |
| Bornhuetter-Ferguson (Cape Cod) | 827,454 | +24.7% |
| Frequency-severity, gamma means | 769,682 | +16.0% |
| Frequency-severity, lognormal means | 655,594 | -1.2% |
| Bootstrap 5% to 95% | 590,325 to 1,168,996 | true value inside |
| Mack s.e. | 208,351 (CV 24%) | error = 0.96 s.e. |

- The point estimates are high, but the true value lies within one standard error and inside the 90% interval.
- The true unpaid amount is a single random outcome. It sits in 149 claims, the 10 largest of which hold 63% of it, and
  the largest single claim is $83,482: infusion claims caught in repeated deny, appeal and deny loops. Month by month, only 6 of 10
  recent months fall inside their own 90% intervals. The ODP gamma noise is too smooth for such lumpy
  outcomes.
- The lognormal frequency-severity figure lands closest, but that is luck rather than skill. Its mean, `exp(mu + sigma^2/2)` with one
  sigma for every lag, sits well below the sample means: $1,579 against $1,947 at lag 1. It came close only because this outcome was low.
- Remittance visibility: 1.22% of payments reach the bank without a visible remittance line (a planted
  defect), so the triangle's natural target is slightly below the true unpaid amount.

**Retrospective test** (`reserve.json` -> `retrospective`, observed data only):

| Earlier as-of | CL estimate | BF estimate | Paid afterwards | CL / observed | BF / observed | Inside CL 90% interval |
|---|---:|---:|---:|---:|---:|---|
| 2024-09-30 | 3,808,920 | 4,165,084 | 4,373,906 | 0.87 | 0.95 | yes |
| 2024-12-31 | 3,937,232 | 3,813,075 | 4,099,394 | 0.96 | 0.93 | yes |
| 2025-03-31 | 4,828,668 | 4,376,559 | 4,588,505 | 1.05 | 0.95 | yes |
| 2025-06-30 | 3,878,439 | 4,503,928 | 4,985,108 | 0.78 | 0.90 | yes |

- All 4 of 4 observed amounts fall inside the interval. The four dates share data, so they are not 4 independent checks.
- Projected cash for the next 3 months against what was actually paid, in order of date: 3.11M vs 3.49M, 3.26M vs 3.42M, 3.95M vs 3.60M, and 3.19M vs 4.16M.
- Bornhuetter-Ferguson's error range of 0.90 to 0.95 is narrower than chain-ladder's 0.78 to 1.05. Both tend to underestimate.

**Frequency and severity tests** (`frequency_severity`):

- Counts: Pearson dispersion is 0.870 (p = 0.86 for over-dispersion). The Cameron-Trivedi alpha is -0.0006 (t = -1.36), and the NB2
  alpha is 0 with LR p = 0.5. **Poisson is adequate.** The slight under-dispersion fits the simulator, which fixes the
  total number of claims.
- Severity: the lognormal fits much better than the gamma by AIC (430,507 vs 478,149); the gamma shape is 0.09, which is extremely skewed.
  Mean severity rises with payment lag, from $1,887 at lag 0 to $5,978 at lag 6 or more. Late payments come from denied and appealed claims,
  which are larger because the planted denial probability rises with charge.

### Where chain-ladder assumptions break

- **The newest period.** At lag 0 the CDF is 8.6, so a noisy first month is multiplied by 8.6. Lag-0 paid
  swings with payment-run timing (see `reserve_factor_stability.png`). In the retrospective test the newest month
  drove most of the error, which is why BF is reported beside CL.
- **IBNR proper.** Claims served late in a month but not submitted by the as-of date are missing from
  the BF exposure. This is why BF also underestimates at month-end retro dates.
- **Changing payment speed.** The simulator's payment speed is constant, and early factors show no trend
  over time. A real payer system change would bias the chain-ladder, and a diagnostic of this kind would be the first warning.
- **Lumpy claims.** A few large late claims dominate the true unpaid amount. Per-month intervals undercover.

### Interview explanation (5 sentences)

I built a paid development triangle in SQL: service month down the side, months since service across
the top, using only payments known on the valuation date, and it ties to the reconciliation to the cent.
Chain-ladder estimates how much each month's payments still grow from the volume-weighted ratios between
consecutive lags, and Mack's formula plus a residual bootstrap give the uncertainty; my code reproduces the
published Mack benchmark exactly. At the as-of date chain-ladder says $864 thousand is still owed, with a 90%
interval of $590 thousand to $1.17 million; the simulator's true answer, $663 thousand, is inside that range but
the point estimate is 30% high, because a handful of large claims make the true outcome lumpy. Re-running at four
earlier dates showed chain-ladder is fragile for the newest month, where it multiplies one noisy month by 8.6, so I
added Bornhuetter-Ferguson, which leans on exposure and narrowed the retrospective error from 0.78-1.05 to 0.90-0.95.
A frequency-severity view showed claim counts are plain Poisson while severity is heavy-tailed and grows with
payment lag, which is the real reason late money matters.
