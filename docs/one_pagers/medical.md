# Receivables Reconciliation & Risk Engine: Medical one-pager

**Summary.** A hospital revenue-cycle engine: every claim line is followed from the claim export,
through the payer's remittance and adjustment codes, to the bank deposit, and tied to the cent.
On top of it sit a CFO view of where cash is stuck, a daily denial work queue and drug margin by
FIFO lot. The hospital ("Meridian Health System"), its payers and patients are fictional and all
records are simulated.

## Business questions answered

- How much did we really collect, and where is cash stuck? (CFO)
- Which denied claims should staff work first today? (revenue-cycle lead)
- Which infusion drugs lose money after reimbursement? (pharmacy)

## Methods

- Identity per claim line: billed = paid + contractual + denied + patient responsibility + open
  balance, enforced by a constraint and by control totals.
- Claim-line state machine over remittance postings; exception queue with reason and owner.
- Denials: logistic overturn model, value = P(overturn) x recoverable - handling cost, zero after
  the appeal window, exact 0/1 knapsack under staff minutes, replay backtest against oldest-first.
- Drugs: FIFO lot costing in SQL, margin decomposition per drug.

## Measured results (simulated)

| Result | Value | Label |
|---|---|---|
| Claim lines reconciled to the cent | <!--n:recon.claim_lines-->72,256<!--/n--> (<!--n:recon.billed_total-->$207,967,496.01<!--/n--> billed) | synthetic_record |
| Unapplied payer credits (true deposits without remittance / queued for manual matching) | <!--n:recon.unapplied.amount-->$2,048,321.11<!--/n--> (<!--n:recon.unapplied.true-->12<!--/n--> / <!--n:recon.unapplied.manual-->52<!--/n--> items) | synthetic_record |
| Denials still workable | <!--n:recon.denied_in_play.amount-->$2,292,185.59<!--/n--> on <!--n:recon.denied_in_play.n-->730<!--/n--> lines | synthetic_record |
| Lines paid below contract | <!--n:recon.underpaid.n-->4,135<!--/n--> lines, <!--n:recon.underpaid.amount-->$360,714.43<!--/n--> | synthetic_record |
| Overturn model AUC: model / reason lookup / generator ceiling | <!--n:q2.auc_model-->0.631<!--/n--> / <!--n:q2.auc_reason-->0.626<!--/n--> / <!--n:q2.auc_planted-->0.644<!--/n--> | model_estimate |
| Today's queue, expected net: knapsack / largest first / oldest first | <!--n:q2.today_value-->$324,591<!--/n--> / <!--n:q2.today_largest-->$311,468<!--/n--> / <!--n:q2.today_oldest-->$30,948<!--/n--> | model_estimate |
| Backtest at 1 hour/day, realised: knapsack / largest first | <!--n:q2.bt1h_knap_realised-->$1,203,967<!--/n--> / <!--n:q2.bt1h_largest_realised-->$1,182,127<!--/n--> (small gain over largest first) | model_estimate |
| Drugs losing money before patient share (mature cohort) | <!--n:q6.n_losing-->21<!--/n--> of <!--n:q6.n_drugs-->25<!--/n--> (<!--n:q6.n_losing_if_patient_pays-->12<!--/n--> if patient share collected) | model_estimate |
| Staff cost per appeal / appeal window | <!--n:q2.appeal_cost-->$45<!--/n--> / <!--n:q2.appeal_window-->90<!--/n--> days | financial_assumption |

## Questions a reader may ask

1. **What is a remittance advice?** The payer's explanation of payment (835/EOB): per claim line
   the allowed, paid and adjusted amounts with group and reason codes (CO contractual, PR patient
   responsibility), grouped under a payment trace number that should match a bank deposit.
2. **Why is the overturn model weak?** Simulated appeal outcomes are mostly noise: even the true
   probabilities reach AUC <!--n:q2.auc_planted-->0.644<!--/n-->. The value comes mainly from claim size.
3. **What is unapplied cash?** Money in the bank with no remittance posted against it. Some is truly
   without a remittance; some is queued because two candidates fit equally well or a deposit looks split. It cannot
   reduce patient or claim balances until the remittance arrives.
4. **Why oldest-first loses, and why largest-first nearly wins.** It works small, old claims first when hours are scarce; the knapsack
   works by expected dollars per minute and drops claims past the appeal window. Largest-first
   comes close to the knapsack because value is dominated by claim size and the overturn model is
   weak.
5. **Is this real hospital data or real work?** No. Fictional hospital, simulated records; the
   workflow mirrors what revenue-cycle teams do.
