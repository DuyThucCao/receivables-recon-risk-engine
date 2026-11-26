%% \newpage
# Glossary

| Term | Meaning |
|---|---|
| 835 / remittance advice (ERA, EOB) | the payer's explanation of a payment: per claim line, allowed, paid and adjusted amounts with reason codes, grouped under a payment trace number |
| 837 / claim | the provider's bill; CMS-1500 (professional) or UB-04 (institutional) |
| Absorbing Markov chain | a chain with states that, once entered, are never left (here PAID, WRITTEN\_OFF) |
| Advance rate | share of eligible collateral a lender will lend against |
| Allowed amount | what the payer recognises for a service under the contract, including the patient's share |
| AUC | probability that a model ranks a random positive above a random negative; 0.5 is chance |
| Bornhuetter-Ferguson | reserve method blending an expected loss ratio with the development pattern |
| Borrowing base | advance rate x eligible collateral - reserves |
| Brier score | mean squared error of probability forecasts; lower is better |
| CARC / RARC | claim adjustment reason code / remittance advice remark code |
| Cash-at-risk | here, mean 13-week receipts minus their 5th percentile |
| Chain-ladder | reserve method projecting cumulative paid with volume-weighted development factors |
| CO / PR | adjustment group codes: contractual obligation (provider absorbs) / patient responsibility |
| Concentration limit | cap on any one obligor's share of the borrowing base |
| Control total | a sum that must equal another sum computed independently |
| Credit balance | payments on a line exceed its charge: money owed back to the payer |
| CVaR | conditional value at risk: mean loss in the worst tail |
| Development triangle | cumulative paid by accident period and development age |
| Exception queue | list of unresolved items, each with type, reason, amount and owner |
| Expected loss | PD x LGD x EAD |
| FIFO | first in, first out: cost administered units from the oldest lot first |
| Float | value of holding someone else's money: amount x rate x time |
| Fundamental matrix | \(N = (I - Q)^{-1}\): expected visits to each transient state |
| IBNR | incurred but not reported (claims that happened but are not yet known or paid) |
| Kaplan-Meier | non-parametric survival curve that uses censored observations |
| Knapsack (0/1) | choose items to maximise value under a capacity; solved exactly by dynamic programming |
| Lockbox | bank service that collects mailed checks and deposits them in bundles |
| Mack standard error | analytic standard error of a chain-ladder reserve |
| NCV | net collectible value: what an open receivable is expected to collect |
| ODP bootstrap | over-dispersed Poisson residual bootstrap of a development triangle |
| Orphan reversal | a reversal whose original posting never reached the hospital |
| PLB | provider-level adjustment or payment in an 835, not tied to one claim |
| Prompt pay | rule requiring payers to pay clean claims within a set time, with interest if late |
| Recoupment | payer takes back money paid earlier |
| Right censoring | the event (payment) has not happened yet; only a lower bound on the lag is known |
| SOFR | Secured Overnight Financing Rate |
| Trace number | identifier of a payer's payment, printed in the remittance and (ideally) the bank description |
| Unapplied cash | money in the bank with no remittance to post it against |
