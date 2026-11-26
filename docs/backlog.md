# Backlog (core)

Status: DONE, PARTIAL, TODO. Acceptance criteria are checked by the named test or output.

| ID | Ticket | Acceptance criteria | Status |
|---|---|---|---|
| C-01 | Calibration file and loader | every entry has value, source_class (one of five labels), source, retrieved; public file overrides denial rates (inside p10-p90), drug list, discount rate; overrides recorded; runs without the public file (`test_core_calibration.py`) | DONE |
| C-02 | Raw-document generator | seven files of DESIGN.md section 3 at about 30k claims / 72k lines, fixed seed, messy formats, defects of section 3 plus hard cases; byte-identical on rerun (`test_core_determinism.py`) | DONE |
| C-03 | Planted structure | per-payer Markov population matrices, payment-lag gamma parameters, denial and overturn logits, underpayment, drug lots/expiry/acquisition ratios, cash defects in `data/truth/planted_parameters.json` | DONE |
| C-04 | Raw and staging | all-text raw tables; typed stg; every raw row staged or rejected with a reason (`RAW_ROWS_ACCOUNTED`, `test_rejects_are_counted_not_dropped`) | DONE |
| C-05 | Core model | per-process facts and dimensions of DESIGN.md section 4 | DONE |
| C-06 | Claim-remittance matching | CR10/CR20/CR30 + DUP10 + REV10 as rule rows; every match stores its rule (`test_every_match_records_its_rule`) | DONE |
| C-07 | Remittance-bank matching | RB10/RB20/RB25/RB30/RB40; lockbox many-to-one; split one-to-many | DONE |
| C-08 | Control identity | per line (CHECK constraint) and in total; independent recomputation; all control totals pass with difference 0.00 (`test_core_tieout.py`) | DONE |
| C-09 | Exception queue | one typed item with reason per unresolved item; ambiguous items labelled AMBIGUOUS | DONE |
| C-10 | close_day | refuses on one-cent corruption of bank amount, deposit match, claim balance, allocation, and out-of-order close (`test_core_close_day.py`); 820 of 820 days close on clean data | DONE |
| C-11 | Q1 marts | cash_position, cash_stuck, ar_aging, pnl_by_payer_service, labelled | DONE |
| C-12 | Pipeline and CLI | `run_all.py` rebuilds from nothing idempotently in about 70 s; REGISTRY runner writes results/<name>.json and summary.md | DONE |
| C-13 | Intake helper | `pipeline.ingest_and_reconcile` lands remittance/bank/claim rows, reconciles incrementally, returns matches, rejects and new exceptions (`test_intake_*`) | DONE |
| C-14 | Evaluation | results/reconciliation.json with precision, recall and exception-queue share per rule and case, plus what the rules cannot resolve | DONE |
| C-15 | Ground-truth isolation | static and runtime tests (`test_core_pipeline_small.py`) | DONE |
| C-16 | Docs | data dictionary, rulebook, decision log, backlog, requests, sql notes | DONE |
| C-17 | Faster close | build back to a few minutes after a 506 s regression: explicit ANALYZE after rebuilds, day-restricted allocations in daily_numbers (docs/sql_notes.md) | DONE |
| C-20 | Requests R-DE-2/3/4 | imputed dispense units flagged; ORPHAN_REVERSAL status; Q7 assumptions in calibration (`test_unreadable_dispense_units_*`, `test_orphan_reversal_lines_*`) | DONE |
| C-18 | Hard-case resolution aids | use payer claim totals (CLP03/CLP04) to resolve identical-line pairs with unreadable claim numbers; suggest (not post) nearest-date candidates for AMBIGUOUS cash | TODO |
| C-19 | Provider-level reconciliation | match periodic interim payments against claim settlements | TODO |
