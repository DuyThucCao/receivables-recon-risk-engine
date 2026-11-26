# Memo: payer underpayment, payment delay and what to do about it

**To:** CFO and revenue-cycle director, Meridian Health System (fictional)
**From:** Economics workstream (Question 7)
**Date:** 2026-11-21. Data as of <!--n:recon.as_of_date-->2026-03-31<!--/n-->; claims with service dates 2024-01-01 to 2025-12-31.

> **Read this first.** Meridian, its six payers (P01-P06) and every claim and payment here are
> **simulated**. Only the SOFR interest-rate series is real public data. The numbers show that the
> method works and recovers the behaviour planted in the simulation. They are **not** findings
> about any real insurer. Every figure is quoted from `results/econ.json` or
> `results/econ_validation.json`; the method is in `docs/model_notes_econ.md`.

## 1. Situation

Payers price each claim line against a negotiated contract. Two things can go wrong for the
hospital even when a claim is "paid":

- the payer allows less than the contract (underpayment);
- the payer pays later than it reasonably should (float: the payer earns interest on the
  hospital's money).

Neither is visible in normal cash posting, because a short or late payment still posts as
"paid". The reconciliation engine already ties every claim line, remittance and deposit to the
cent. This memo builds on it to answer three questions: who underpays and by how much, what delay
is worth to payers, and why a payer would do either.

## 2. Findings

**Underpayment is concentrated in one payer.** We compare each paid line's allowed amount with
the contract amount. Patient deductibles and coinsurance are part of the allowed amount, so they
are not counted as shortfall: <!--n:q7.patient_at_contract-->$5,647,877.90<!--/n--> of patient share sits on lines priced exactly at
contract. On that basis the total shortfall at first pricing is **<!--n:q7.shortfall-->$334,486.29<!--/n-->**. Ranked by
shortfall rate (shortfall / contract value, 95% interval resampling whole claims):

| Payer | Claims underpaid | Shortfall rate | Shortfall $ (95% interval) |
|---|---:|---:|---:|
| P03 | <!--n:q7.p03.share-->13.9%<!--/n--> | <!--n:q7.p03.rate-->1.61%<!--/n--> | <!--n:q7.p03.usd-->$143,952.69<!--/n--> (<!--n:q7.p03.usd_lo-->$102,969.87<!--/n--> to <!--n:q7.p03.usd_hi-->$194,322.14<!--/n-->) |
| P06 | <!--n:q7.p06.share-->10.6%<!--/n--> | <!--n:q7.p06.rate-->0.75%<!--/n--> | <!--n:q7.p06.usd-->$40,791.20<!--/n--> (<!--n:q7.p06.usd_lo-->$24,142.76<!--/n--> to <!--n:q7.p06.usd_hi-->$60,110.39<!--/n-->) |
| P05 | <!--n:q7.p05.share-->7.7%<!--/n--> | <!--n:q7.p05.rate-->0.45%<!--/n--> | <!--n:q7.p05.usd-->$30,851.75<!--/n--> (<!--n:q7.p05.usd_lo-->$20,012.57<!--/n--> to <!--n:q7.p05.usd_hi-->$45,509.59<!--/n-->) |
| P02 | <!--n:q7.p02.share-->5.5%<!--/n--> | <!--n:q7.p02.rate-->0.41%<!--/n--> | <!--n:q7.p02.usd-->$60,752.25<!--/n--> (<!--n:q7.p02.usd_lo-->$30,832.32<!--/n--> to <!--n:q7.p02.usd_hi-->$106,861.40<!--/n-->) |
| P04 | <!--n:q7.p04.share-->2.0%<!--/n--> | <!--n:q7.p04.rate-->0.25%<!--/n--> | <!--n:q7.p04.usd-->$23,286.77<!--/n--> (<!--n:q7.p04.usd_lo-->$4,028.11<!--/n--> to <!--n:q7.p04.usd_hi-->$49,192.84<!--/n-->) |
| P01 | <!--n:q7.p01.share-->4.4%<!--/n--> | <!--n:q7.p01.rate-->0.18%<!--/n--> | <!--n:q7.p01.usd-->$34,851.63<!--/n--> (<!--n:q7.p01.usd_lo-->$24,476.40<!--/n--> to <!--n:q7.p01.usd_hi-->$46,370.40<!--/n-->) |

P03 stands out on every measure. Its rate interval does not overlap any other payer's. The
other payers' intervals overlap, so their order among themselves is not reliable. Separately,
later repricing cut allowed amounts by a further <!--n:q7.reduced_usd-->$38,404.73<!--/n--> on <!--n:q7.reduced_lines-->747<!--/n--> lines, while <!--n:q7.overpaid_lines-->485<!--/n--> lines were
*over*paid by <!--n:q7.overpaid_usd-->$46,586.92<!--/n--> (money the hospital may owe back).

**The method recovers what was planted.** Line-level flags match the simulation's records with
precision <!--n:q7.flag_precision-->0.9988<!--/n--> and recall <!--n:q7.flag_recall-->1.0000<!--/n-->. The planted underpayment probability lies inside the 95%
interval for <!--n:q7.planted_share_inside-->5<!--/n--> of 6 payers. The miss, P03, is the simulation's own sampling noise: the sample
realised <!--n:q7.p03_realised-->13.875%<!--/n--> against a planted <!--n:q7.p03_planted-->15%<!--/n-->, and the estimate tracks the sample. The planted mean
shortfall lies inside the interval for <!--n:q7.planted_mean_inside-->6<!--/n--> of 6 payers.

**Delay is worth less than underpayment, and much of it follows denials.** At the monthly SOFR
rate, payers held the hospital's cash worth **<!--n:q7.float_total-->$363,534.00<!--/n-->** in total, of which **<!--n:q7.float_late-->$179,197.70<!--/n-->**
was beyond a <!--n:q7.prompt_pay_days-->30<!--/n-->-day prompt-payment window (an assumed standard). Per $100,000 paid, late float is
highest at P03 (<!--n:q7.p03.late_per100k-->$433.47<!--/n-->) and P06 (<!--n:q7.p06.late_per100k-->$432.82<!--/n-->) and lowest at P01 (<!--n:q7.p01.late_per100k-->$195.09<!--/n-->). For four of the six
payers, most of the late float comes from claims paid only after a denial was appealed or
corrected. In effect, a denial that is later reversed works as a delay. Median first-pass payment
times match the simulation's planted lags.

**Most of the recoverable money is on few, large claims.** Appealing costs about <!--n:q7.appeal_cost-->$45<!--/n--> per claim
(an assumption) and wins with probability *w*. So a claim is worth appealing when shortfall x *w*
> <!--n:q7.appeal_cost-->$45<!--/n-->, which gives a threshold of <!--n:q7.appeal_cost-->$45<!--/n--> / *w*. Using each payer's observed denial-appeal win rate
(<!--n:q7.pwin_min-->36%<!--/n-->-<!--n:q7.pwin_max-->48%<!--/n-->) as *w*, the thresholds run from **<!--n:q7.threshold_min-->$93.23<!--/n-->** to **<!--n:q7.threshold_max-->$126.40<!--/n-->**. **<!--n:q7.above_threshold-->$300,904.07<!--/n-->**
of the shortfall sits on claims above these thresholds. Expected recovery after appeal costs is
**<!--n:q7.net_recovery-->$104,252.32<!--/n-->**, of which P03 is <!--n:q7.p03_net_recovery-->$39,142.75<!--/n-->. Underpayment disputes cite a written contract rate,
so they probably win more often than denial appeals. At an assumed <!--n:q7.pwin_alt-->90%<!--/n--> win rate the threshold
falls to <!--n:q7.threshold_alt-->$50.00<!--/n-->, and P03 alone would yield <!--n:q7.p03_alt_recovery-->$112,692.64<!--/n-->.

## 3. Why would a payer do this? (economic reasoning, not an estimate)

A payer that underprices a claim by *s* keeps the money unless the hospital appeals and wins. If
losing an appeal only means paying what was owed anyway, underpricing is a free option. The payer
keeps *s* whenever the hospital does not appeal or loses, and pays nothing extra when it is
caught. To make underpricing unprofitable, a payer appealed on every claim would have to repay
*more* than the shortfall: 1/*w* - 1 times *s* on top, which is **<!--n:q7.penalty_min-->1.07<!--/n-->x to <!--n:q7.penalty_max-->1.81<!--/n-->x** across these
payers. An interest clause of <!--n:q7.clause_rate-->12%<!--/n--> a year (an illustrative figure) adds only <!--n:q7.clause_share-->2.96%<!--/n--> of *s* over 90
days. So **interest clauses do not deter underpricing**.

Delay is different. Holding a dollar 30 days earns the payer about <!--n:q7.float_gain_cents-->0.38<!--/n--> cents at SOFR. With a <!--n:q7.clause_rate-->12%<!--/n-->
interest clause, the same delay would *cost* the payer about <!--n:q7.clause_cost_cents-->0.61<!--/n--> cents. **An enforced
prompt-pay interest clause above the payer's cost of funds removes the gain from paying late.**

The relationship also repeats: contracts are renegotiated. If the payer weights the next contract
period at <!--n:q7.renewal_weight-->0.9<!--/n--> (illustrative), P03's annual gain from shortfall plus late float, <!--n:q7.p03_annual_gain-->$88,890.10<!--/n-->, stops
paying once the payer expects to lose at least <!--n:q7.p03_renewal_loss-->$9,876.68<!--/n--> a year at renewal. That small amount
explains why **documented, aggregated evidence matters**. A payer that knows the hospital measures
underpayment payer by payer and brings it to the renewal table faces a credible future cost. A
payer that expects isolated, small appeals does not.

## 4. Recommendation

1. **Audit P03 first, then P06 and P05.** P03 has the highest underpayment rate (<!--n:q7.top_rate-->1.61%<!--/n-->; interval
   <!--n:q7.top_rate_ci-->1.15% to 2.15%<!--/n-->) and the most dollars. Ask for a re-pricing of all P03 claims above the threshold
   (<!--n:q7.p03_n_above-->154<!--/n--> claims, <!--n:q7.p03_above_usd-->$129,417.85<!--/n-->).
2. **Adopt a per-payer appeal threshold of <!--n:q7.appeal_cost-->$45<!--/n--> / P(win)**: about <!--n:q7.threshold_min-->$93.23<!--/n--> to <!--n:q7.threshold_max-->$126.40<!--/n--> today, recomputed
   quarterly from appeal outcomes. Below the threshold, do not appeal claim by claim. Log the
   shortfall and raise it in aggregate at renewal.
3. **Contract terms at renewal:**
   - a prompt-pay interest clause above market rates, applied automatically, which neutralises
     the float gain;
   - an underpayment true-up clause with a penalty of at least the shortfall (or audit-cost
     recovery), because recovery alone does not deter;
   - the same interest applied from the original due date when a denial is overturned, which
     removes the gain from "deny, then pay";
   - audit rights to re-price a sample of claims each year.
4. **Track bunching.** If a payer's shortfalls start to cluster just below the hospital's
   threshold, that is the signature of strategic underpricing. The engine can test this every
   month.
5. **Link to drugs (Question 6).** Under the simulation's assumptions, the drug book loses money
   because <!--n:q6.denied_share-->25.1%<!--/n--> of drug contract dollars end in a final denial. It turns profitable below
   <!--n:q6.breakeven-->14.4%<!--/n--> (`results/drug.json`). In the sensitivity tables, cutting denials from 25% to 15% moves
   the margin from <!--n:q6.sens_d25-->-$4,769,531.05<!--/n--> to <!--n:q6.sens_d15-->-$251,894.83<!--/n-->, while raising contracts from 125% to 135% of
   the limit moves it from <!--n:q6.sens_c125-->-$4,991,896.62<!--/n--> to <!--n:q6.sens_c135-->-$2,299,104.14<!--/n-->. Denial prevention on drug claims
   therefore matters at least as much as rates. P04's contract rate is below acquisition cost for <!--n:q6.p04_below_cost-->11<!--/n--> drugs; raise those rates in
   the same negotiation.

## 5. Risks and limits

- **Simulated data.** The simulated underpayments are random draws, not strategic choices. The
  incentive argument is therefore reasoning about how a payer *could* respond, not an effect
  measured in this data.
- **Win probability is borrowed.** Underpayment disputes do not appear in the claim feed. The
  base case uses denial-appeal win rates, and the <!--n:q7.pwin_alt-->90%<!--/n--> case is an assumption; recovery estimates
  scale with this choice.
- **Assumptions.** The <!--n:q7.appeal_cost-->$45<!--/n--> appeal cost, the <!--n:q7.prompt_pay_days-->30<!--/n-->-day window, the <!--n:q7.clause_rate-->12%<!--/n--> clause and the <!--n:q7.renewal_weight-->0.9<!--/n--> renewal
  weight are illustrative. SOFR is a near risk-free rate, so it understates what a payer earns on
  held cash; float values are a lower bound. Lines still unpaid at the snapshot are left out of
  float and reported separately.
- **Small payers, wide intervals.** P04's shortfall interval runs from <!--n:q7.p04.usd_lo-->$4,028.11<!--/n--> to <!--n:q7.p04.usd_hi-->$49,192.84<!--/n-->; rank
  payers other than P03 with care.
- **Relationship risk.** Aggressive auditing can sour renewal talks. Present the evidence as
  contract compliance, aggregated, not as accusation.
