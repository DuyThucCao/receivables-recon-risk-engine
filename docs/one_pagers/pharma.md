# Receivables Reconciliation & Risk Engine: Pharma one-pager

**Summary.** The life of a physician-administered drug after it leaves the wholesaler: bought by
lot and expiry, administered, billed and reimbursed weeks later, if the claim is not denied. FIFO
lot costing in SQL ties every lot to the cent, and margin per drug is split exactly into price
gap, waste, denials, pending payment, patient share and pricing below contract. Reference prices
are the real CMS Medicare Part B payment limits; purchase prices, volumes and payments are
simulated for a fictional hospital.

## Business questions answered

- Which drugs lose money after reimbursement, and why (price, waste, or denials)?
- Which payer contracts pay below acquisition cost?
- How much stock expires, and on which drugs?

## Methods

- FIFO by receipt date as a range-overlap join on cumulative units (no loops); expired units
  charged to their own lot; telescoping cent rounding so each lot sums to its invoice value.
- Margin = payer paid - FIFO cost - expired cost, mature cohort (service date at least <!--n:q6.mature_days-->180<!--/n--> days
  before the as-of date, an assumption).
- Comparison with the pharmacist's recorded lot (specific identification) and with the
  generator's physical lot records.

## Measured results (simulated, except labelled reference prices)

| Result | Value | Label |
|---|---|---|
| Lots; identity purchased = administered + expired + on hand | <!--n:q6.n_lots-->1,292<!--/n-->; units <!--n:q6.lot_identity_units-->yes<!--/n-->, cents <!--n:q6.lot_identity_value-->yes<!--/n--> | synthetic_record |
| Purchased / administered / expired / on hand ($) | <!--n:q6.usd_purchased-->$47,252,467.95<!--/n--> / <!--n:q6.usd_administered-->$44,017,867.00<!--/n--> / <!--n:q6.usd_expired-->$89,817.65<!--/n--> / <!--n:q6.usd_on_hand-->$3,144,783.30<!--/n--> | synthetic_record |
| Lots matching generator on every quantity | <!--n:q6.lots_exact_share-->99.38%<!--/n--> | model_estimate |
| Drugs losing money before patient share | <!--n:q6.n_losing-->21<!--/n--> of <!--n:q6.n_drugs-->25<!--/n--> (<!--n:q6.n_losing_if_patient_pays-->12<!--/n--> if patient share collected) | model_estimate |
| Contract minus cost / lost to denials | <!--n:q6.price_gap-->$12,264,030.41<!--/n--> / <!--n:q6.denied_leak-->$12,791,364.27<!--/n--> | model_estimate |
| Largest loss | <!--n:q6.top_loser-->J1303<!--/n-->, <!--n:q6.top_loser_margin-->-$2,382,174.03<!--/n-->, <!--n:q6.top_loser_denied_share-->34%<!--/n--> of lines denied | model_estimate |
| Drugs with P04 contract rate below FIFO cost | <!--n:q6.p04_below_cost-->11<!--/n--> | model_estimate |
| Reference price file | CMS Part B payment limits, <!--n:public.asp_quarter-->2026Q4<!--/n-->, <!--n:public.n_drugs-->25<!--/n--> J-codes | published_aggregate |

## Questions a reader may ask

1. **Is the ASP payment limit the hospital's cost?** No. It is a Medicare reimbursement reference;
   acquisition cost depends on contracts, discounts and 340B status, none of which is in the file.
2. **Why do expensive drugs lose most?** In the simulator, denial odds rise with claim amount, so
   that pattern is built in. The method is the result, not a fact about <!--n:q6.top_loser-->J1303<!--/n-->.
3. **FIFO vs specific identification?** Nearly identical here (<!--n:q6.fifo_cost-->$42,558,392.10<!--/n--> vs <!--n:q6.si_cost-->$42,558,392.75<!--/n-->)
   because the simulated wholesaler price changes once.
4. **What about partial-vial waste?** The dispense log has no discarded-amount records, so vial
   waste cannot be measured; only expiry waste is.
5. **What would you need from real data?** Actual acquisition prices, 340B flags, JW/JZ waste
   records, payer drug fee schedules and denial reasons by drug.
