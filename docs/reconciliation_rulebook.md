# Reconciliation rulebook

Rules are rows in `recon.match_rule` (rule id, stage, method, priority, tolerance, date window,
active). `recon.run_matching(stage)` runs the active rules of a stage in priority order and each
match row stores its `rule_id`. A rule only ever matches rows that are still unmatched, and it only
accepts a candidate that is **unique**; anything ambiguous goes to `recon.exception_queue` with a
reason starting `AMBIGUOUS:` instead of being guessed. Changing a tolerance or switching a rule off
is a data change (`update recon.match_rule ...`), not a code change.

Examples below are real rows from the generated data set (seed 20260331, as-of 2026-03-31; raw
row numbers refer to `data/raw/*.csv`). All records are synthetic. Matching quality per rule and per
case is in `results/reconciliation.json` and `results/summary.md`.

Order of a run (`recon.run_all`): duplicates -> claim to remittance -> reversal pairing ->
payment totals -> remittance to bank -> balances -> claim-line state machine -> exception queue
-> control totals -> daily reconciliation. `recon.close_day(d)` then verifies each day.

## 1. Duplicates

### DUP10_SAME_TRACE_IDENTICAL (priority 10, tolerance 0.00)
A remittance line repeated verbatim within the same payment (`trace_key`, same `line_hash` over all
raw fields) is a file duplicate: the first copy is kept, later copies go to `recon.remit_duplicate`
and are never posted. **Exception:** a claim can legitimately bill the same service twice; if the
payer echoes no line control number, both remittance lines are verbatim identical. Copies are
therefore allowed up to the number of identical claim lines (same normalised claim id, procedure,
date and charge); only copies beyond that are duplicates. With a line control number, a line may
appear once.

*Example (duplicate).* Payment `P01:1100827712`, raw rows 44593 and 44594 are identical
(claim MHS240012577, 29881, paid 4,252.83). Row 44594 is excluded; the payment header equals the
sum of the remaining lines, which is what proves the copy was not real money.
*Example (kept).* Payment `P01:2365394744` carries two identical lines for claim MHS240006546,
80053, 95.00, no line control number. The claim has two such lines (2 and 4), so both are kept and
posted, one to each line.
*Limit.* If the claim number on such a pair is unreadable (transposed), the claim cannot be found,
so only one copy is allowed and the second is wrongly excluded. One such case occurs in the data.

## 2. Claim to remittance

Line resolution (CR10 and CR20): once the claim is found, the line is the one whose line item
control number equals the echoed one; otherwise the line with the same procedure, service date and
charge (absolute value, because reversals are negative). Two guards: a candidate is **ruled out** if
the remittance carries a line control number or member id that contradicts it; and when several
identical sibling lines qualify, the k-th such remittance line within one payer claim payment (same
payer claim number and status) is paired with the k-th sibling by line number.

### CR10_EXACT_CLAIM_ID (priority 10)
`patient_control_no` equals `claim_id` exactly.
*Example.* Raw row 1: MHS240000012, 85025, line control MHS240000012-01, charge 55.00, paid 23.50
-> claim MHS240000012 line 1.

### CR20_NORMALISED_CLAIM_ID (priority 20)
Normalised identifiers are equal: upper case, letters and digits only, leading `PCN`/`CLM`/`CLAIM`/
`REF` removed, leading zeros removed (`stg.norm_id`).
*Example.* Raw rows 41-42 print `000MHS240000133` (zero padding). Normalised: `MHS240000133` ->
claim lines 1 (93306) and 2 (93000) by line control number.

### CR30_MEMBER_DATE_AMOUNT (priority 30, tolerance 0.00, window 0 days)
Fallback when the identifier fails: same normalised member id, service date within the window,
charge within tolerance. Ranking: echoed line control number agrees (0), then procedure agrees (1),
else (2); the best rank must be unique.
*Example.* Raw rows 19-20 print `MHS240000906`, a transposition of MHS240000096 (no such claim
exists). Member GBP870102298, 2024-01-03, 96365 at 520.00 and J2357 at 7,792.00 -> claim
MHS240000096 lines 1 and 2.
*Ambiguous.* `UNMATCHED_REMIT:34297`: identifier MHS240021752 matches no claim and two claim lines
share member GBP141869682, date 2024-11-05 and charge 55.00 (the patient had two visits that day for
the same test) with no line control number. Queued: "AMBIGUOUS: ... not matched automatically".
A person resolves it with the payer claim number (ICN) on the payer portal.

### Why the contradiction guard exists
During development (on an earlier version of the generated data; that results file was not kept), evaluation found 7 false matches in CR10: a transposed claim number happened to
equal another real claim from the same day, and the line was resolved on procedure + date +
charge (same lab test, same price). The echoed line control number and member id both
contradicted the match. With the guard those lines fall through to CR30 and match correctly
(decision D-010).

## 3. Reversals

### REV10_REVERSAL_PAIR (priority 10)
A reversal (claim status 22) is paired with the earlier posting on the same claim line whose paid,
allowed and denied amounts it negates exactly; the k-th reversal pairs with the k-th such posting.
Rebuilt every run into `recon.reversal_link`. Unpaired reversals become `ORPHAN_REVERSAL`.
*Example.* Claim line 12 (claim MHS240000005 line 1, 99283): row 256 on 2024-01-26 denies 450.00;
row 4144 on 2024-03-15 reverses it (denied -450.00) and row 4148 pays it after the appeal. State
history: SUBMITTED -> DENIED -> REVERSED -> PAID, every transition allowed.
*Limit.* An orphan reversal means the posting it reverses never reached us. In this data every
orphan is explained: the original's remittance file was withheld (money arrived as unapplied
cash), or the original row was rejected in staging.

## 4. Remittance to bank

A payment (`recon.remit_trace`) is matched to bank credits of category `PAYER_EFT` or `LOCKBOX`.
`header_total` (what the payer said it paid) is what must reach the bank.

### RB10_TRACE_EXACT (priority 10, tolerance 0.00, window 6 days)
Trace extracted from the description equals the trace; amount equal; posting between payment
date - 1 and + window. Unique on both sides.
*Example.* `P05:8250443050`, 168.26 dated 2024-01-12 -> bank row 14, 2024-01-12,
"ACH CREDIT SORREL BAY CARE HCCLAIMPMT TRN*1*8250443050*1900000505\".

### RB20_TRACE_NORMALISED (priority 20)
Bank trace equals the trace without leading zeros, or is a truncated prefix of at least 6
characters. *Example.* `P02:3614891389`, 36,466.01 -> bank row 59 whose description is cut at
"...TRN*1*36148913" because the payer's long ACH company name fills the field.

### RB25_TRACE_SPLIT (priority 25)
One payment arriving as two or more credits that all carry its trace: the credits must sum to the
payment and belong to no other payment. *Example.* `P01:1512100316`, 86,371.86 dated 2024-04-02 ->
two credits (49,478.07 on 2024-04-03 and the remainder), both with TRN 1512100316.
*Limit.* If the second credit has no trace, nothing ties it to the payment: both credits go to
`UNAPPLIED_CASH` with reason "POSSIBLE SPLIT OR PARTIAL DEPOSIT: trace ... belongs to payment ... of
..., but this credit is ..." and the payment to `REMIT_NO_DEPOSIT`. All 8 such payments in the data
are queued, none matched.

### RB30_LOCKBOX_SUBSET (priority 30, window 10 days)
A lockbox deposit "ITEMS n" equals the sum of exactly n unmatched check payments dated in the window
before the deposit; the combination must be unique (bounded search: at most 20 candidates, 8 items).
*Example.* `P06:98418241`, 2,462.17 dated 2024-02-07 -> bank row 67, 2024-02-12,
"LOCKBOX DEPOSIT LBX 40217 ITEMS 1".

### RB40_PAYER_AMOUNT_WINDOW (priority 40, window 4 days)
EFT credit with no usable trace: payer recognised from the ACH company name, amount equal, posting
in the window, unique on both sides. *Example.* `P05:7850548707`, 23,856.77 -> bank row 449,
"ACH CREDIT SORREL BAY CARE HCCLAIMPMT" (no addenda).
*Ambiguous.* The government plan pays a fixed 20,000.00 provider-level advance twice a week. When
two consecutive advances both arrive without a trace, two credits fit two payments equally well;
both credits are queued "AMBIGUOUS: credit 20000.00 from P04 with no trace fits 2 unmatched
payment(s) of the same amount in the date window ...". Picking the nearest date would usually be
right, but it is a guess, so the rule does not do it.

## 5. Variance

### UP10_BELOW_CONTRACT (tolerance 1.00)
On a line the payer priced (`payer_allowed` > 0), contract allowed minus payer allowed above the
tolerance is an `UNDERPAYMENT` exception. Contract allowed = rate x units (fee schedule) or charge
x percent, lesser of billed charge. *Example.* MHS240000002 line 1, P03, 99284, billed 780.00:
contract 350.46, payer allowed 277.51 -> underpaid 72.95.

## 6. Exception queue

| Type | Severity | Meaning | Who acts |
|---|---|---|---|
| UNMATCHED_REMIT | HIGH | remittance line tied to no claim line; reason says ambiguous, line rejected in staging, or not our patient | cash posting |
| DUPLICATE_REMIT_LINE | INFO | verbatim copy excluded from posting | none |
| ORPHAN_REVERSAL | MEDIUM | reversal without the posting it reverses | cash posting |
| ILLEGAL_TRANSITION | HIGH | a posting moved a line along a path the state machine forbids | cash posting |
| CREDIT_BALANCE | HIGH | postings exceed the charge (often a duplicate payer payment awaiting recoupment) | refunds |
| UNDERPAYMENT | MEDIUM | payer allowed below contract | contract variance team |
| TRACE_TOTAL_MISMATCH | HIGH | payment header differs from its posted lines (a line rejected in staging) | cash posting |
| UNAPPLIED_CASH | HIGH | payer money in the bank with no remittance (reason: no remittance, AMBIGUOUS, or POSSIBLE SPLIT) | cash posting |
| REMIT_NO_DEPOSIT | HIGH | payer says it paid, no deposit past the in-transit window | treasury |
| IN_TRANSIT | INFO | payment within the in-transit window (timing difference) | none |
| PROVIDER_LEVEL_PAYMENT | INFO | provider-level (PLB) payment held in suspense, not claim money | reimbursement |
| STALE_REVERSAL | MEDIUM | line reversed and not reposted within 30 days | follow-up |
| NO_RESPONSE | MEDIUM | no remittance 90 days after submission, claim not written off | follow-up |
| UNKNOWN_REASON_CODE | LOW | adjustment code not in the reference table (treated as denial) | data steward |
| STAGING_REJECT | HIGH/LOW | raw row rejected (HIGH for claims, remittance, bank unless a pure repeat) | data steward |

Each item has a stable `exception_key` (TYPE:id). A run re-opens items it still finds and marks
the rest `CLEARED`, so the queue shows what changed after new data arrives.

## 7. Control identity and daily close

Per claim line, enforced by a CHECK constraint and recomputed independently in the controls:
`billed = paid + contractual + denied + patient_resp + open_balance`. Cash, per payment and in
total: `payments = matched deposits + unmatched remittance (REMIT_NO_DEPOSIT) + timing differences
(IN_TRANSIT, deposits on another day)`; bank side: `payer credits = allocated + unapplied cash`.
Unresolved items always sit in an explicit bucket, so the totals tie to the cent even when the rules
decline to match.

`recon.close_day(d)` refuses (raises, nothing written) if the previous day is not closed,
`daily_recon` is stale, the statement does not chain row by row, a deposit or payment is not fully
allocated, the remittance side leaves a difference, any unmatched item of that day is missing from
the queue, or a claim line posted that day fails the identity or differs from a fresh sum of its
postings. Tests corrupt one cent in each place and check the refusal.

## 8. What the rules cannot resolve automatically

| Case | Outcome | Why |
|---|---|---|
| transposed claim number + twin visit (same member, date, code, charge), no line control number | queued AMBIGUOUS | nothing in the remittance says which visit was paid |
| identical repeated services + unreadable claim number | second copy excluded as duplicate | the claim, which would show two lines, cannot be found |
| split payment, second credit without trace | both credits unapplied, payment without deposit | the untraced credit matches nothing on amount |
| equal-amount payments, credits without trace | queued AMBIGUOUS | two credits fit two payments equally |
| reversal of a posting we never received | ORPHAN_REVERSAL | the original remittance is missing |
| remittance for a claim line rejected in staging | UNMATCHED_REMIT | the line never reached core; fix the claim and rerun |

These are realistic limits of rule-based matching, and the reason a reconciliation team exists.
The numbers measure consistency between this project's generator and its rules; they are not
evidence of matching accuracy on real remittances.
