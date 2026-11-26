%% \newpage
# Reconciliation rulebook

Rules run in priority order within a stage; a rule only matches rows still unmatched and only
accepts a **unique** candidate. Anything ambiguous goes to `recon.exception_queue` with a reason
starting `AMBIGUOUS:`. The order of a run (`recon.run_all`) is: duplicates, claim to remittance,
reversal pairing, payment totals, remittance to bank, balances, claim-line state machine,
exception queue, control totals, daily reconciliation. The examples are real rows of the generated
data set (all synthetic).

## Duplicates: DUP10_SAME_TRACE_IDENTICAL

A remittance line repeated verbatim within the same payment (same hash over all raw fields) is a
file duplicate: the first copy is kept, later copies go to `recon.remit_duplicate` and are never
posted. The exception: a claim can legitimately bill the same service twice, and if the payer
echoes no line control number both remittance lines are identical. Copies are therefore allowed
up to the number of identical claim lines.

*Example.* Payment `P01:1100827712`, raw rows 44593 and 44594 are identical (claim MHS240012577,
29881, paid 4,252.83). Row 44594 is excluded; the payment header equals the sum of the remaining
lines, which proves the copy was not real money.

Detection quality against the planted truth: <!--n:recon.dup.flagged-->356<!--/n--> lines flagged for <!--n:recon.dup.planted-->355<!--/n--> planted duplicates,
precision <!--n:recon.dup.precision-->0.9972<!--/n-->, recall <!--n:recon.dup.recall-->1.0000<!--/n-->. The one false positive is a repeated service whose claim number
was unreadable, so the claim with two identical lines could not be found.

## Claim to remittance: CR10, CR20, CR30

| Rule | Logic | Example |
|---|---|---|
| `CR10_EXACT_CLAIM_ID` | echoed patient control number equals the claim id; line by line control number, else procedure + date + charge | raw row 1: MHS240000012, 85025, line control MHS240000012-01 -> claim line 1 |
| `CR20_NORMALISED_CLAIM_ID` | equal after upper-casing, removing punctuation, PCN/CLM/CLAIM/REF prefixes and leading zeros | raw rows 41-42 print `000MHS240000133` -> claim MHS240000133 lines 1 and 2 |
| `CR30_MEMBER_DATE_AMOUNT` | fallback: same normalised member id, service date, charge; echoed line control number, then procedure break ties; unique best rank required | raw rows 19-20 print MHS240000906 (transposed, no such claim) -> member, date and charges identify MHS240000096 |

Two guards apply to CR10 and CR20. A candidate is ruled out when the remittance carries a line
control number or member id that contradicts it. And when several identical sibling lines
qualify, the k-th such remittance line within one payer claim payment is paired with the k-th
sibling by line number.

The contradiction guard exists because evaluation found false
matches during development: a transposed claim number happened to equal another real claim from
the same day, and the line was resolved on procedure, date and charge because it was the same lab
test at the same price. The echoed line control number and member id both disagreed. With the
guard those lines fall through to CR30 and match correctly (decision D-010). This is the clearest
example of the evaluation changing the rules, rather than the rules being fitted to the score.

## Reversals: REV10_REVERSAL_PAIR

A reversal (claim status 22) is paired with the earlier posting on the same claim line whose paid,
allowed and denied amounts it negates exactly; the k-th reversal pairs with the k-th such posting.
Unpaired reversals become `ORPHAN_REVERSAL`. *Example:* claim MHS240000005 line 1 (99283): row 256
denies 450.00 on 2024-01-26; row 4144 reverses the denial on 2024-03-15 and row 4148 pays the line
after the appeal. State history: SUBMITTED, DENIED, REVERSED, PAID, every transition allowed. All
<!--n:recon.orphans-->76<!--/n--> orphan reversals in the data are explained by a withheld original remittance or a row rejected in
staging (<!--n:recon.orphans_unexplained-->0<!--/n--> unexplained).

## Remittance to bank: RB10 to RB40

| Rule | Logic | Example |
|---|---|---|
| `RB10_TRACE_EXACT` | trace in the bank description equals the trace; amount equal; posting in the window | `P05:8250443050`, 168.26 -> bank row 14 with `TRN*1*8250443050*...` |
| `RB20_TRACE_NORMALISED` | trace without leading zeros, or a truncated prefix of at least 6 characters | `P02:3614891389` -> description cut at `TRN*1*36148913` |
| `RB25_TRACE_SPLIT` | one payment arriving as two or more credits that all carry its trace and sum to it | `P01:1512100316`, 86,371.86 -> two credits on 2024-04-03 |
| `RB30_LOCKBOX_SUBSET` | a lockbox deposit "ITEMS n" equals the sum of exactly n unmatched check payments in the window; unique combination (bounded search) | `P06:98418241` -> "LOCKBOX DEPOSIT LBX 40217 ITEMS 1" |
| `RB40_PAYER_AMOUNT_WINDOW` | EFT credit with no trace: payer from the ACH company name, equal amount, unique in the window | `P05:7850548707`, 23,856.77 -> credit with no addenda |

## Variance: UP10_BELOW_CONTRACT

On a line the payer priced, contract allowed minus payer allowed above a $1.00 tolerance is an
`UNDERPAYMENT` exception. Contract allowed is rate x units (fee schedule) or charge x percent,
lesser of billed charge where the contract says so. *Example:* MHS240000002 line 1, P03, 99284,
billed 780.00: contract 350.46, payer allowed 277.51, underpaid 72.95. The current build has
<!--n:recon.underpaid.n-->4,135<!--/n--> such lines, <!--n:recon.underpaid.amount-->$360,714.43<!--/n--> in total.

## Matching quality against planted truth

@@table:rules@@

**Claim side, by planted case.**

@@table:claim_cases@@

**Bank side, by planted case.**

@@table:bank_cases@@

Overall, on self-generated defects: claim to remittance precision <!--n:recon.c2r.precision-->1.0000<!--/n-->, recall <!--n:recon.c2r.recall-->0.9998<!--/n--> on postable lines, with <!--n:recon.c2r.missed-->18<!--/n-->
postable lines missed; remittance to bank precision <!--n:recon.r2b.precision-->1.0000<!--/n-->, recall <!--n:recon.r2b.recall-->0.9616<!--/n-->.

## What the rules cannot resolve automatically

| Case | Outcome | Why |
|---|---|---|
| transposed claim number and a twin visit (same member, date, code, charge), no line control number | queued AMBIGUOUS | nothing in the remittance says which visit was paid |
| identical repeated services and an unreadable claim number | second copy excluded as duplicate | the claim, which would show two lines, cannot be found |
| split payment whose second credit has no trace | both credits unapplied, payment without deposit | the untraced credit matches nothing on amount |
| two equal provider payments, credits without trace | queued AMBIGUOUS | two credits fit two payments equally well |
| reversal of a posting never received | ORPHAN_REVERSAL | the original remittance is missing |
| remittance for a claim line rejected in staging | UNMATCHED_REMIT | the line never reached core; fix the claim and rerun |

Recall below one on these cases is a design choice, not a defect.
Picking the nearest date for two equal untraced payments would usually be right, but a wrong cash
posting misstates two customer balances and is hard to find later, while a queued item costs a
person a few minutes with the payer portal. These limits are also the reason a reconciliation
team exists. The scores measure consistency between this project's generator and its own rules;
they are not evidence of accuracy on real remittance files.
