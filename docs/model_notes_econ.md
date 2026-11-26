# Model notes: drugs (Q6) and economics (Q7)

All numbers below are copied from `results/drug.json`, `results/drug_validation.json`,
`results/econ.json` and `results/econ_validation.json` (seed 20260331, as-of date 2026-03-31).
The data are **simulated** for a fictional hospital and six fictional payers. Real public inputs:
the drug names, HCPCS codes and CMS Medicare Part B payment limits (label `published_aggregate`),
and the SOFR series (label `observed_public`). Purchase prices, volumes, claims and payments are
simulated (`synthetic_record`). So the results show two things only: that the methods recover
what the generator planted, and that the decision rules behave correctly. They say nothing
about real drugs or real payers.

How to reproduce:

```bash
export RRE_PG_DIR=~/.cache/receivables-engine/pg-econ
python run_all.py --skip-generate     # build and reconcile
python scripts/run_models_econ.py     # models, validation, figures
python -m pytest tests/test_drug*.py tests/test_econ*.py
```

---

## Q6. Which drugs lose money after reimbursement? (`models/drug.py`)

**Who asks.** The pharmacy manager.

**Data.** `core.fact_drug_purchase` (one row per invoice line = one lot: units, invoice value,
expiry), `core.fact_drug_dispense` (administrations and expired-waste events), the drug claim
lines in `core.fact_claim_line`, their reconciled balances in `recon.claim_line_balance` and
statuses in `recon.claim_line_status`. Reference price: CMS Part B payment limit per billing
unit, read straight from `config/public_calibration.yaml` (section `drug_reference_prices`, 25 drugs,
`published_aggregate`) into `mart.drug_reference_price`. The model reads no generator settings:
acquisition cost comes only from the simulated invoices.

**Method.**

1. *FIFO lot costing in SQL* (`sql/models/61_drug_fifo.sql`), no loops:
   - An expired-waste event names its lot, so its units are charged to that lot (an expiry is a
     physical fact about one lot).
   - Each lot gets a range on the drug's cumulative-units axis, in receipt order:
     `cum_end = SUM(capacity) OVER (PARTITION BY drug ORDER BY received, lot)`,
     `cum_start = cum_end - capacity`, with capacity = units purchased - units expired.
   - Administrations get ranges the same way, ordered by time.
   - Allocation is a range-overlap join: event *e* uses lot *l* for
     `min(e.end, l.end) - max(e.start, l.start)` units whenever the ranges overlap. One event
     can span two lots (partial lot); one lot serves many events.
   - Dollars to the cent: inside a lot with invoice value *V* and *U* units, the pieces in order
     (administrations, expired units, units on hand) have cumulative units q_1..q_n = U, and
     `cost_k = round(V q_k / U, 2) - round(V q_(k-1) / U, 2)`. The sum telescopes to exactly *V*.
2. *Margin per drug* (`sql/models/62_drug_margin.sql`), for service dates at least 180 days
   before the as-of date (cohort MATURE, so most claims have settled; the 180 days is an
   assumption):
   `margin = P - C - W` with *P* payer paid (reconciled, net of reversals), *C* FIFO cost of
   administered units, *W* FIFO cost of expired units. With *E* the contract amount and *R* the
   patient share, the margin splits exactly into
   `margin = (E - C) - W - R - denied - pending - pricing_gap`,
   where each claim line's gap `E - P - R` goes to *denied*, *pending* or *pricing_gap* by its
   status. The largest negative term is reported as the drug's main drag on margin.
3. *Break-even final-denial rate and sensitivity.* Let d = D / E be the dollar-weighted
   final-denial rate (contract value on lines whose final status is DENIED, divided by all
   contract value). Hold the collection ratio on non-denied dollars at its observed value
   k = P / (E - D). Then `margin(d) = k E (1 - d) - C - W`, and margin = 0 at
   `d* = 1 - (C + W) / (k E)`. With the contract set to a multiple t of the CMS payment limit,
   E is replaced by t x limit x units. `results/drug.json` (`sensitivity_mature`) and
   `figures/drug_margin_sensitivity.png` show total margin for d from 5% to 30% and t from 100%
   to 150%.

**Validation** (`results/drug.json` `validation`, `results/drug_validation.json`).

- Inventory identity holds on all 1,292 lots, in units and to the cent
  (`lot_identity_units_all_lots` = true, `lot_identity_value_all_lots` = true). Purchased
  $47,252,467.95 = administered at FIFO cost $44,017,867.00 + expired $89,817.65 + on hand
  $3,144,783.30.
- Against the generator's physical lot records: purchased, administered (823,394), expired
  (3,154) and on-hand (58,776) units agree exactly for every drug, expired units agree on every
  lot, and 99.38% of lots agree on every quantity. 1,616 administered units sit on a different
  lot of the same drug, because the generator consumes first-expiry-first-out while FIFO goes by
  receipt date; 201 units are placed by FIFO on a lot after its expiry date (flagged
  `fifo_infeasible_units`).
- FIFO cost $42,558,392.10 versus $42,558,392.75 using the lot each pharmacist recorded, over
  6,071 administrations: the cost-flow convention barely matters here because the simulated
  wholesaler price changes only once (on 2025-01-01).
- Hand-made example in `tests/test_drug_fifo.py` (partial lot, expiry, thirds of a cent, later
  data not moving earlier allocations, an infeasible FIFO allocation, over-dispensing).

**Results (MATURE cohort), and what they rest on.** The headline holds only under the
simulation's stated assumptions:

- contract rates average 123%-129% of the CMS payment limit per drug (the generator draws
  100%-135% by payer);
- acquisition cost is 85%-107% of the limit;
- the simulated dollar-weighted final-denial rate on drug claim lines is **25.1%**
  (`denied_dollar_share_observed` = 0.2514), at the upper end of the published issuer range.

Under these assumptions, 21 of 25 drugs lose money, and the drug book turns profitable when the
dollar-weighted final-denial rate falls below **14.4%** (`breakeven_denial_share_total` = 0.1444).
All 21 losing drugs have a break-even rate below their simulated rate.

Payer paid $33,820,889.95 against FIFO cost $38,621,452.39 and expired waste $30,350.26: margin
-$4,830,912.70. The contract would pay $12,264,030.41 more than the drugs cost (`price_gap`), so
the loss is driven by the denial assumption, not by price. Denied drug claims remove
$12,791,364.27 of contract value. Smaller terms are patient share not counted as collected
($3,471,232.73), pending payment ($615,992.07) and the pricing gap ($186,003.78).

Sensitivity (`sensitivity_mature`), with contracts as simulated:

| Final-denial rate | 5% | 10% | 15% | 20% | 25% | 30% |
|---|---:|---:|---:|---:|---:|---:|
| Total margin ($) | 4,265,741.38 | 2,006,923.27 | -251,894.83 | -2,510,712.94 | -4,769,531.05 | -7,028,349.15 |

At the simulated denial rate, with every contract set to the same multiple of the payment limit:

| Contract, % of limit | 100% | 115% | 125% | 135% | 150% |
|---|---:|---:|---:|---:|---:|
| Total margin ($) | -11,723,877.82 | -7,684,689.10 | -4,991,896.62 | -2,299,104.14 | 1,740,084.59 |

A real hospital would see different results. 340B acquisition prices (well below the payment
limit) or higher commercial rates would move the break-even well above 14.4%, and its own denial
rate may be lower.

Largest loss: J1303 (ravulizumab), -$2,382,174.03. Its simulated dollar-weighted denial rate is
33.0% against a break-even of 16.9%. In the generator, denial odds rise with claim amount, so the
most expensive drug is denied most often. Expired waste is small and concentrated in J2777
($27,108.77, average shelf life at receipt 91.7 days). One payer-level price problem does exist:
P04's contract rate is below FIFO cost for 11 drugs (loss at contract -$61,605.52,
`contract_below_cost_by_payer`).

**Limitations.**
- Purchase prices are simulated as a fixed ratio to the CMS payment limit, so the "cost versus
  ASP" picture is built in, not discovered. The ASP limit is a Medicare reference, not cost.
- Patient share is treated as not collected because patient payments are not in the data.
  `margin_if_patient_pays` gives the other bound.
- Denials in the MATURE cohort include some still under appeal, so the denial drag is slightly
  overstated.
- The break-even and sensitivity are linear counterfactuals. They hold the non-denied collection
  ratio (88.8%) and costs fixed, and they do not model how denial rates would respond to the
  hospital's own actions.
- No partial-vial waste: the dispense log has no discarded-amount (JW-type) records, so vial-size
  waste cannot be measured.
- 5 dispense rows with unreadable units have their units imputed from the claim line. This
  workaround stays in `60_drug_inputs.sql` until core loads those rows itself (request R-DE-2,
  still open).

**Interview explanation (5 sentences).** I cost every drug administration with FIFO, written as
a set-based SQL join: each lot and each administration gets a range on a cumulative-units axis,
and the overlap of two ranges is the quantity taken from that lot. Cents are allocated by rounding
cumulative amounts, so each lot ties out exactly: purchased = administered + wasted + on hand, and
I test that on all 1,292 lots and on a hand-worked example. Margin per drug is reconciled payer
cash minus FIFO cost minus expired waste, split exactly into price gap, waste, denials, pending
payment, patient share and pricing below contract. Under the simulation's assumptions (contracts
about 123-129% of the CMS limit, acquisition 85-107%, and a 25.1% dollar-weighted final-denial
rate), 21 of 25 drugs lose money. The book turns profitable below a 14.4% denial rate, so the
loss is driven by the denial assumption, not by price. The data are simulated, so this shows the
method works and recovers the generator's lots; a hospital with 340B pricing or higher commercial
rates would see different numbers.

---

## Q7. Which payers underpay against contract, by how much, and why would they? (`models/econ.py`)

**Who asks.** A consulting economist advising the hospital.

**Data.** `recon.claim_line_balance` (contract amount from `core.dim_contract_rate`, net allowed,
paid, patient share), the matched remittance lines (`core.fact_remit_line`) for first versus
later pricing (provider-level PLB rows, `is_provider_level = true`, are excluded),
`core.fact_claim_event` for appeal outcomes, FRED SOFR (monthly mean).

### a. Underpayment

**Method** (`sql/models/63_econ_underpayment.sql`). For each claim line the payer priced, compare
in *allowed* terms, because the contract's allowed amount includes the patient's deductible and
coinsurance:
`pricing_shortfall = contract - first allowed` if it is at least $0.01 (assumption), else 0.
Lines are classified, first match wins: PRICED_BELOW_CONTRACT; REDUCED_AFTER_PAYMENT (later
repricing or downward adjustment); PARTIAL_PENDING (allowed but not yet paid; timing); OVERPAID;
AT_CONTRACT. Pending payment is measured in allowed terms (allowed - patient share - paid), so a
reversal whose original posting is missing (core exception `ORPHAN_REVERSAL`) cannot look like
money still owed. Denied lines are excluded: a denial is a different decision (Q2). The data contain
no bundling or partial-line timely-filing adjustment codes on paid lines, so no shortfall can be
attributed to them. Per payer: share of claims underpaid, shortfall rate = sum(shortfall) /
sum(contract), dollars, and the mean shortfall fraction of underpaid claims. Intervals come from
a percentile bootstrap with 2,000 replicates that resamples **whole claims** (all lines of a claim
share one pricing decision). Each statistic is a ratio of resampled sums.

**Validation** (`results/econ_validation.json`).
- Line flags against the generator: precision 0.9988, recall 1.0; shortfall $334,486.29 versus
  the generator's $334,462.50.
- Planted underpayment probability inside the 95% interval for 5 of 6 payers. The miss is P03:
  planted 0.15, estimate 0.13899 (0.12941-0.14926). The simulated sample itself realised 0.13875,
  so the estimator tracks the sample and the gap is the generator's own sampling noise.
- Planted mean shortfall inside the interval for 6 of 6 payers. Ranking by estimated shortfall
  rate puts the planted worst payer (P03) first; the bottom two (P01, P04) are swapped.

**Results.** Total shortfall at first pricing $334,486.29. P03 has the highest rate: 1.61% of
contract (1.15%-2.15%), $143,952.69 ($102,969.87-$194,322.14), 13.9% of its priced claims.
Patient responsibility on lines priced at contract ($5,647,877.90) is correctly *not* counted.
Later reductions add $38,404.73 on 747 lines. Overpayments are -$46,586.92 on 485 lines.

### b. Value of float

**Method** (`sql/models/64_econ_float.sql`). `float = paid x r x days / 365`, with r = average
SOFR in the month of payment, and days from submission to first payment. "Late" means days beyond
a 30-day window (an assumption, not a statute). Split by path: first-pass versus paid after a
denial.

**Validation.** Median days to first-pass payment match the generator's realised lags for all
payers (model 21/27/30/43/29/40 days versus generator 21/27/30/43/29/39).

**Results.** Total float $363,534.00, of which $179,197.70 is beyond the 30-day window. Per
$100,000 paid, late float is highest for P03 ($433.47) and P06 ($432.82) and lowest for P01
($195.09). For four of six payers, most late float comes from claims paid only after a denial:
on these numbers, a denial that is later overturned works as a delay.

### c. Incentive reading (economics, not causal estimates)

**Model.** A payer that underpays a claim by *s* keeps it unless the hospital appeals
(probability *q*) and wins (*w*). If it loses it repays *s* plus a penalty pi s:
`gain = s - q (w (1 + pi) s + c_payer)`. Underpaying stops paying only if
`pi >= 1/(q w) - 1`. The hospital appeals when `s w > c`, so the threshold is `s* = c / w`, with
c = $45 (assumption in config) and *w* = each payer's denial-appeal win rate from the claim feed
(a stand-in: underpayment disputes are not in the data).

**Validation.** Estimated appeal win rates contain the planted overturn targets for 6 of 6
payers. Formula tests are in `tests/test_econ_formulas.py`.

**Results.** Thresholds run from $93.23 (P01) to $126.40 (P03) per claim. $300,904.07 of the
shortfall sits on claims above their payer's threshold, with expected net recovery after appeal
costs of $104,252.32. If disputes win 90% of the time (sensitivity), the threshold falls to $50,
and P03 alone yields $112,692.64. Pure recovery never deters a payer whose underpayments are not
all appealed and won. The penalty needed with q = 1 is 1.07 to 1.81 times the shortfall, against
2.96% of *s* for a 12%-a-year interest clause over 90 days. Late payment is different: a payer
gains about 0.37% per dollar paid 30 days late at SOFR, and with a 12% clause it would lose about
0.61% instead. So an interest clause can deter delay, but it cannot deter underpricing.
Repeated game: if the payer weights next contract period at 0.9, P03's annual gain of $88,890.10
stops paying once the payer expects to lose at least $9,876.68 a year in worse renewal terms.

**Limitations.** Simulated underpayments are random draws, not strategic. So the data cannot show
the payer behaviour this model describes, such as shortfalls bunched just below the hospital's
appeal threshold. On real data that bunching would be the test. P(win) for disputes is borrowed
from denial appeals. The 30-day window, the 12% clause and the 0.9 discount factor are
illustrative assumptions, held in `econ.ASSUMPTIONS` until core moves them into the config
(request R-DE-4, still open). Float on lines still unpaid at the snapshot is censored and is reported
separately (`mart.float_value_open`). SOFR is a near risk-free rate, so it understates a payer's
actual return on cash.

**Interview explanation (5 sentences).** I compare each paid claim line's allowed amount with the
contract amount. Using allowed rather than paid means deductibles and coinsurance are never
counted as underpayment, and denials, pending partial payments and later repricing are separated
out. Per payer I report the share of claims underpaid and the shortfall rate. Their 95% intervals
come from a bootstrap that resamples whole claims, because the lines of one claim share one
pricing decision. On the simulation, the line flags match the generator with 99.9% precision and
100% recall, and the planted rates fall inside the intervals for 5 of 6 payers. The economics
part says that if being caught only means paying what was owed anyway, underpaying is a free
option for the payer. The hospital should appeal only above c / P(win), and only a penalty that
is a multiple of the shortfall, or the threat of losing the contract, changes the payer's
calculation.
