# App: receivables reconciliation and risk engine

A Streamlit app that runs fully offline against the embedded PostgreSQL database built by the
pipeline. One screen per decision question and job title; there is no overview screen. Every number
on screen is read from the pipeline (the `mart` tables and `results/*.json`) or recomputed by calling
the model's own function. All records are simulated for a fictional hospital, Meridian Health
System, and six fictional payers.

## Launch (one command)

```bash
python app/launch.py
```

Open http://localhost:8501. The launcher binds to localhost, turns off Streamlit's usage statistics
(no network calls) and passes extra arguments to `streamlit run`, e.g. `--server.port 8600`.
`~/.venvs/receivables-engine/bin/streamlit run app/streamlit_app.py` also works.

The database must exist first (about 5 minutes, once):

```bash
python run_all.py            # or --skip-generate to reuse data/raw
```

The app uses the same database location as the pipeline: `RRE_PG_DIR` (default
`~/.cache/receivables-engine/pg`) or `DATABASE_URL`.

## The demo copy and Reset

The app never writes to the pipeline database (`receivables`). On first start it makes a copy,
`receivables_demo`, with `CREATE DATABASE ... TEMPLATE receivables STRATEGY FILE_COPY` (under a
second). The reconciliation desk lands new rows in the copy; **Reset demo database** drops it and
copies the baseline again, so a demo can be repeated exactly. When `run_all.py` or a model script
rebuilds the baseline, the next app start notices (fingerprint stored as a database comment) and
refreshes the copy. With `DATABASE_URL` (hosted Postgres) the app reads that database directly and
Reset is disabled. Close model scripts before pressing Reset: Postgres cannot copy a database that
another session is using.

## Screens

| Screen | Job title | Question | Reads |
|---|---|---|---|
| Reconciliation desk | Revenue-cycle / cash-posting analyst | Did today's payments post, and which items need a person? | `pipeline.ingest_and_reconcile`, `recon.*`, `mart.cash_stuck` |
| Q1 | Hospital CFO | How much did we really collect, and where is cash stuck? | `mart.cash_position`, `cash_stuck`, `ar_aging`, `pnl_by_payer_service`, `recon.daily_recon` |
| Q2 | Revenue-cycle lead | Which denied claims should staff work first today? | `mart.work_queue`, `work_queue_backtest`; slider calls `prioritize.choose` |
| Q3 | Treasurer | Will we be short of cash in the next 13 weeks? | `mart.cash_forecast*`, `results/cashflow.json`; runs `cashflow.forecast` and `cashflow.shortfall_metrics` |
| Q4 | Payer actuary | How much is still owed on claims incurred but not yet paid? | `mart.dev_triangle`, `reserve_factors`, `reserve_by_period`, `reserve_retro` |
| Q5 | Bank credit analyst | How much can we lend against these receivables? | `mart.borrowing_base*`; inputs call `credit.eligibility` and `credit.borrowing_base_table` |
| Q6 | Pharmacy manager | Which drugs lose money after reimbursement? | `mart.drug_margin`, `drug_lot_position` |
| Q7 | Consulting economist | Which payers underpay against contract, by how much, and why would they? | `mart.underpayment*`, `float_value`, `econ_appeal_outcome`; appeal cost calls `econ.appeal_threshold` |
| Methods | Anyone checking the work | Where does each number come from, and what can simulated data show? | `mart.table_label`, `docs/data_sources.md`, `results/` |

Every chart and table states its data label (`synthetic_record`, `model_estimate`,
`financial_assumption`, `observed_public`, `published_aggregate`) and its source table. Controls that
change an assumption (staff hours, cash need, advance rate, concentration limit, age limit, appeal
cost) carry the `financial_assumption` badge. If a model has not written its marts yet, its screen
shows "Not built yet: run run_all.py" instead of failing.

## A two-minute demo

1. **Reconciliation desk -> Sample batches -> Generate a fresh batch.** The batch pays a few open
   claims and contains one of each defect: a mangled claim number, a line priced below contract, a
   denial, a claim number the hospital never billed, and a bank credit with no remittance. Download
   the two CSVs to show the raw format, then **Run remittance + bank**.
2. Read the result: matches by rule (CR10, CR20, RB10), the three new exceptions with their reasons,
   the day closed by `recon.close_day`, and the control identity before and after (24 checks tie to
   the cent).
3. Open **Q1 Hospital CFO**: payer paid and the unapplied-cash bucket moved by exactly the batch.
4. **Run remittance only** on another batch to show *in transit*, then **Run bank only** to clear it.
   **Run the replay** to show that re-sent files post nothing (duplicates and repeated bank references).
5. **Reset demo database** and repeat.

## Code layout

```
app/
  launch.py             one-command launcher
  streamlit_app.py      navigation (st.navigation), one page per screen
  rre_app/
    demo_db.py          demo copy of the database and its reset
    loaders.py          one data-loading function per screen (no Streamlit; tested)
    batches.py          sample inputs in the raw export formats
    state.py            Streamlit caching keyed on the database state
    ui.py               data-label badges, number formats, chart style
    views/              desk, cfo, revcycle, treasurer, actuary, credit, pharmacy, economist, methods
```

Tests: `python -m pytest tests/test_app.py tests/test_reports.py`
(they use a separate throw-away copy, `receivables_apptest`, so a running app is not disturbed).

The CFO PDF report is built separately: `python scripts/build_reports.py`
writes `reports/cfo_report.pdf` (`src/recon_engine/reports/cfo_report.py`).
