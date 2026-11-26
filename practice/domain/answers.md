# Domain questions: answers

All data are simulated; SQL is in `solutions/`.

## 1. Revenue cycle (`dom1_revenue_cycle_trace.sql`)

The hospital submits a claim (CMS-1500 or UB-04 lines; here `claims_export.csv`). The
clearinghouse acknowledges it and the payer reviews it (events SUBMITTED, IN_REVIEW). The payer
adjudicates and sends a remittance advice (835): here a denial, claim status 4, with a reason
code such as CO-197 (no prior authorisation); the line's open balance falls to zero because the
denied amount is now one of its components. Staff appeal (event APPEALED). If the appeal wins, the
payer sends a reversal of the denial (claim status 22, negative denied amount), which re-opens the
balance, and then a new payment line; money arrives in the bank under the payment's trace number.
The claim-line state machine records SUBMITTED -> DENIED -> REVERSED -> PAID, and every
transition must be in `recon.line_status_transition`. The query shows the events, then each
remittance posting with the rule that matched it.

## 2. Remittance codes (`dom2_remittance_codes.sql`)

CO, contractual obligation: the provider absorbs the amount and may not bill the patient (CO-45 is
the normal discount to the contracted allowance; CO-197, CO-50 and others are denials). PR, patient
responsibility: deductible (PR-1), coinsurance (PR-2), copay (PR-3), billed to the patient. The
difference matters because PR dollars are still collectible, from a different party and with more
bad-debt risk, while CO dollars are gone unless the denial is overturned. The query shows CO-45 is
by far the largest adjustment by dollars; denials follow, led by prior authorisation and
administrative codes. All shares come from the simulated generator settings.

## 3. Borrowing base (`dom3_borrowing_base_from_aging.sql`)

A borrowing base is the amount a lender will advance against collateral, here receivables:
advance rate x eligible receivables, minus reserves. Usually ineligible: items past an age limit
(often 90 or 120 days), denied or disputed items, credit balances, government receivables without
an assignment, and the part of any one obligor above a concentration limit. The reason is that
the lender only wants collateral that will turn into cash quickly and predictably. At the as-of
date the simulated hospital has not billed for three months, so almost the whole book is older
than 120 days and the eligible base is close to zero. The project's credit model therefore also
reports a certificate on <!--n:credit.certificate_date-->2025-09-30<!--/n-->, when the hospital was still billing: availability
<!--n:credit.availability-->$2,656,726<!--/n-->.

## 4. IBNR (`dom4_ibnr_triangle.sql`)

IBNR, incurred but not reported, is the liability for claims that have happened but are not yet
fully known or paid; in practice the reserve covers incurred-but-not-paid amounts too. A
development triangle puts accident (service) periods down the side and development age across the
top, with cumulative paid in each cell. Chain-ladder computes volume-weighted age-to-age factors
f(d) = sum C(d+1) / sum C(d), multiplies them into cumulative factors and projects each period's
latest diagonal to ultimate. Assumptions: each period develops like earlier ones (stable payment
pattern), factors do not depend on the period's size, and there is no development after the last
observed age (no tail). The project's monthly triangle gives a chain-ladder unpaid estimate of
<!--n:q4.cl_unpaid-->$863,526<!--/n-->; the simulator's true value is <!--n:q4.true_unpaid-->$663,429<!--/n-->.

## 5. Prompt pay (`dom5_prompt_pay.sql`)

Prompt-pay laws and contract clauses require payers to pay clean claims within a set number of
days and add interest when they do not; the windows and rates differ by state and contract, so
30 days and 12% here are illustrative. Late payment has value because the payer earns interest on
money it owes (float), while the hospital funds its operations meanwhile. Measure it as paid
amount x rate x days late / 365, on clean claims only, because a denied-then-paid claim is late for
a different reason. The project values all float at SOFR: <!--n:q7.float_total-->$363,534.00<!--/n--> in total, of which
<!--n:q7.float_late-->$179,197.70<!--/n--> is beyond the <!--n:q7.prompt_pay_days-->30<!--/n-->-day window.
