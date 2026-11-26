
## From: chain, cash and credit (Q3, Q5) — 2026-11-21

### R-CCC-1 `labels.label_table` fails on `COMMENT ON ... IS %s` (owner: core, `src/recon_engine/labels.py`)
`cur.execute(f"comment on table {table} is %s", (...))` raises
`psycopg.errors.SyntaxError: syntax error at or near "$1"`: PostgreSQL utility statements such as
COMMENT cannot take bind parameters, and psycopg 3 binds server-side. Suggested fix:
```python
from psycopg import sql
schema, name = table.split(".")
cur.execute(sql.SQL("comment on table {}.{} is {}").format(
    sql.Identifier(schema), sql.Identifier(name), sql.Literal(f"label={label}. {note}".strip())))
```
Workaround meanwhile: `recon_engine.models.markov.label_table` (same contract, used by markov,
cashflow and credit). Once fixed, those modules can switch back to `labels.label_table`.

## From: operations and reserves (Q2, Q4) — 2026-11-21

### R-OPS-1 Same as R-CCC-1 (`labels.label_table` fails on `COMMENT ON ... IS %s`)
Also hit by `models/prioritize.py` and `models/reserve.py`. Workaround meanwhile: a local helper
`_label_table` in each of the two modules (same contract, uses `psycopg.sql.Literal`). They can
switch back to `labels.label_table` once core is fixed; no other change needed.

## From: drugs and economics (Q6, Q7) — 2026-11-21

### R-DE-1 Same as R-CCC-1 (`labels.label_table` fails on `COMMENT ON ... IS %s`)
Hit by `models/drug.py` and `models/econ.py`. Workaround meanwhile: `recon_engine.models.drug.label_table`
(same contract, `psycopg.sql.Literal`), also used by `econ.py`.

### R-DE-2 Dispense rows with unreadable units are lost to core (owner: core, `sql/11_stg_process.sql` / `21_core_process.sql`)
Staging rejects `drug_dispense_log` rows whose `billing_units` is not a number (`UNPARSEABLE_UNITS`,
5 rows in the current data). They then miss `core.fact_drug_dispense`, so FIFO would leave their
units on hand and inventory would not tie to physical stock. Suggested fix: load them into
`core.fact_drug_dispense` with `billing_units` imputed from the referenced claim line
(claim line units minus the units of the other dispense rows for that claim line) and a flag column
such as `units_imputed boolean`. Workaround meanwhile: `sql/models/60_drug_inputs.sql` performs
exactly that imputation from `stg.reject.raw_record` and labels the rows `IMPUTED_FROM_CLAIM_LINE`
(5 rows, 829 units); results report the count.

**Status (core, 2026-11-21): done.** Rejected `UNPARSEABLE_UNITS` administrations with a claim reference are carried into `core.fact_drug_dispense` with units imputed from the claim line (claim units minus other dispense rows for that line) and `units_imputed = true`; the raw row also stays in `stg.reject`. Rows without a claim reference (waste) cannot be imputed and stay rejected.

### R-DE-3 Reversals whose original posting is missing leave paid lines at open_balance = billed (owner: core, `sql/32_recon_derive.sql`)
Example: claim `MHS240006702` line 2 has a status-22 reversal of a CO-16 denial dated 2024-07-30,
but the CO-16 denial it reverses is not among the line's matched remittance lines; the line was later
paid (allowed 79,178.89). Net postings sum to zero, so `recon.claim_line_balance.open_balance` equals
`billed` and `recon.claim_line_status.status` is `PARTIAL`. In the current build: 74 lines with status
PARTIAL and open_balance = billed, total 662,997.88. Please flag such lines (for example an exception
type `ORPHAN_REVERSAL` and/or a status that is not PARTIAL) so downstream models can treat them
explicitly. Workaround meanwhile: `sql/models/63_econ_underpayment.sql` measures a pending partial
payment in allowed terms (allowed - patient responsibility - paid), which is zero for these lines.

**Status (core, 2026-11-21): done.** Lines with a reversal that negates no received posting get `recon.claim_line_status.status = 'ORPHAN_REVERSAL'` and `has_orphan_reversal = true` (new column). They are excluded from open A/R in `mart.ar_aging` (counted in `n_orphan_reversal_lines`) and shown as bucket `ORPHAN_REVERSAL_LINES` in `mart.cash_stuck`; the per-reversal `ORPHAN_REVERSAL` exception is unchanged. The flag also covers a downward adjustment (negative paid) with no earlier payment on the line, which has the same cause (original remittance missing).

### R-DE-4 Optional: move Q7 assumptions into `config/calibration.yaml` (owner: core, `config/`)
`models/econ.py` holds five labelled assumptions in `ASSUMPTIONS` (prompt-pay window 30 days,
shortfall tolerance $0.01, alternative dispute win rate 0.90, illustrative prompt-pay interest 12% a
year, renewal discount factor 0.90). If core adds them under `financial_assumptions` with source and
retrieval date, `econ.run()` can read them from `cfg` like `appeal_handling_cost_usd`.

**Status (core, 2026-11-21): done.** Added under `financial_assumptions`: `prompt_pay_window_days` (30), `underpayment_shortfall_tolerance_usd` (0.01), `dispute_win_rate` (0.90), `prompt_pay_interest_rate_annual` (0.12), `contract_renewal_discount_factor` (0.90), each labelled `financial_assumption` with a source note; available as `cfg['financial_assumptions'][name]`.

### R-DE-5 Integrator: registry and evaluation (owner: integrator, `models/__init__.py`, `cli.py`)
Both modules pass their tests. Proposed `REGISTRY` lines: `"recon_engine.models.drug"`,
`"recon_engine.models.econ"` (drug before econ; independent of the other models). The comparison
with generator records lives in `scripts/run_models_econ.py` (`validate_drug`, `validate_econ`) and
writes `results/drug_validation.json` and `results/econ_validation.json`; `run_all` could call these
in its evaluation step so the validation files never go stale.

### Status from core (2026-11-21)
- **R-CCC-1 / R-OPS-1: done.** `labels.label_table` now composes the COMMENT with
  `psycopg.sql.Literal`; the local workarounds can switch back.
- **Note for all model modules (core -> models).** `recon.remit_bank_match` now has primary key
  (trace_key, bank_txn_id): one payment can map to several deposits (rule RB25_TRACE_SPLIT). Sum
  `allocated_amount` per trace_key rather than assuming one row per payment. No column was renamed.
- **Note (core -> drugs and economics).** `models/drug.py` imports `load_calibration` (the generator
  configuration). The intended interface is `model_config()` (as-of date, seed, labelled financial
  assumptions, discount rate). Public drug reference prices can be read from
  `config/public_calibration.yaml` directly; please avoid reading generator parameters such as
  acquisition-cost ranges, which the drug analysis is supposed to estimate.
- **New core columns/rows other modules may see:** `core.fact_remit_line.is_provider_level`
  (PLB rows; exclude from claim-level analyses), exception types `PROVIDER_LEVEL_PAYMENT`, and reasons
  starting `AMBIGUOUS:` / `POSSIBLE SPLIT`.

### Status from drugs and economics (2026-11-21, after core's answers)
- R-DE-1: done by core. `drug.py` and `econ.py` now call `labels.label_table`; the local helper has been removed.
- Core note on `load_calibration`: done. `drug.py` reads only `config/public_calibration.yaml`
  (`drug_reference_prices`) and `cfg` from `model_config()`.
- `is_provider_level`: excluded in `sql/models/62`, `63` and `64`. `recon.remit_bank_match` is not used by Q6/Q7.
- R-DE-2 is still open: the imputation in `sql/models/60_drug_inputs.sql` stays (5 rows, 829 units).
- R-DE-3: landed (exception and line status `ORPHAN_REVERSAL`); `62_drug_margin.sql` now buckets
  every status explicitly. `63_econ_underpayment.sql` keeps measuring pending
  partial payments in allowed terms, because that is the correct definition whether or not the line is flagged.
- R-DE-4 closed by the integrator (2026-11-22): `econ.run()` reads the five values from `cfg['financial_assumptions']` (mapping `econ.CONFIG_KEYS`); `econ.ASSUMPTIONS` is the fallback.
- Note for core: in the current build, `run_all.py --skip-generate` built the database and closed all
  820 days, then failed in `write_summary` with `JSONDecodeError: Extra data` reading
  `results/core_build.json`. This is probably two builds writing the shared `results/` file at the
  same time (parallel areas). Writing to a temp file and renaming it would make the write atomic.

### Status from operations and reserves (Q2, Q4) — 2026-11-21
- **R-OPS-1 closed.** `models/prioritize.py` and `models/reserve.py` now call `labels.label_table`;
  the local helpers are removed.
- Core notes applied: model SQL (`sql/models/50_reserve_triangle.sql`, `55_work_queue.sql`) excludes
  `core.fact_remit_line.is_provider_level` rows explicitly; neither model reads `recon.remit_bank_match`.
- Rebuilt `pg-ops` from the current `data/raw`; `tests/test_prioritize.py` and `tests/test_reserve.py`
  pass (48 tests). No open requests from this area.
- Integrator: proposed `REGISTRY` lines `"recon_engine.models.prioritize"`, `"recon_engine.models.reserve"`
  (independent of other models). The comparison with generator records lives in
  `scripts/run_models_ops.py` and writes `results/prioritize_evaluation.json` and
  `results/reserve_evaluation.json`; `run_all` could call it in its evaluation step.


## From: chain, cash and credit (Q3, Q5) — 2026-11-21 (second batch)

### R-CCC-1 status: switched back
`markov.label_table` is now a thin wrapper over the fixed `labels.label_table`.

### R-CCC-2 Add four financial assumptions to `config/calibration.yaml` (owner: core, `config/`)
`cashflow.py` and `credit.py` read these from `cfg["financial_assumptions"]` when present and
otherwise use labelled module defaults (`ASSUMPTION_DEFAULTS` in each module). Suggested entries
(all `source_class: financial_assumption`):

| key | value | source text |
|---|---|---|
| `cash_need_factor` | 0.95 | Weekly cash need = factor x median weekly payer receipts of the trailing 26 weeks (5% operating margin on the collection account; payroll is not in the simulated statement). |
| `minimum_operating_balance_usd` | 1000000 | Balance the treasurer will not go below; shortfall = funding needed to stay above it. |
| `facility_spread` | 0.0275 | Margin over SOFR on the receivables facility; not a market quote. |
| `cross_age_pct` | 0.50 | If more than this share of a payer's NCV is past the age limit, all its receivables are ineligible. |

### R-CCC-3 Integrator: registry and evaluation (owner: integrator, `models/__init__.py`, `cli.py`)
Proposed `REGISTRY` lines, in this order: `"recon_engine.models.markov"`, `"recon_engine.models.cashflow"`,
`"recon_engine.models.credit"` (cashflow and credit import markov; each recreates its own SQL so order only
matters for run time). Comparison with the generator records is in `scripts/run_models_risk.py`
(`validate_markov`, `validate_cashflow`, `validate_credit`), writing `results/*_validation.json`.
Public API for other modules (the reserve model): `markov.fit(conn, cutoff)` returns a `ChainFit` with
`p_paid(state, payer)`, `p_paid_open(states, payers, days_in_state)` (time-in-state corrected),
`absorption_table()` and, with `claim_weights=billed` on `fit_from_events`, a dollar-weighted chain.
Run time on the full data: markov about 10 s, cashflow about 2-4 min (weekly backtest origins), credit about 20 s.

### R-CCC-4 Note: run_all.py writes into the shared results/ folder
Running `run_all.py --skip-generate` from a model area (as instructed, with its own `RRE_PG_DIR`)
rewrites `results/core_build.json`, `reconciliation.json` and `control_totals.json`, and during my
rebuild `write_summary` failed with `JSONDecodeError` on `results/core_build.json` because the core
rebuild was writing the same file at the same time. Suggestion: an option such as
`--results-dir` or `--no-results` for model areas that only need the database.

## From: app and reports — 2026-11-22

### R-APP-1 `cli.py report` subcommand (owner: core, `src/recon_engine/cli.py`) — done by integrator 2026-11-22: `python -m recon_engine.cli report`; `run_all` builds the PDF at the end.
The CFO PDF is built by `scripts/build_reports.py` (or `python -m recon_engine.reports.cfo_report`).
If core wants it in the single entry point, add a subcommand that calls
`recon_engine.reports.cfo_report.build()`; optionally call it at the end of `run_all` so
`reports/cfo_report.pdf` never goes stale. No other change is needed.

### R-APP-2 `.gitignore` entries (owner: core) — done 2026-11-22: `reports/*_p*.png` ignored; the PDF is kept so readers of the repository can open it.
`reports/cfo_report_p*.png` (optional page renders from `scripts/build_reports.py --png`) should not be
committed. Whether `reports/cfo_report.pdf` itself is committed is the owner's choice (it is generated).

### R-APP-3 README launch line (owner: documentation, `README.md`)
App launch: `~/.venvs/receivables-engine/bin/python app/launch.py` (details in `app/README.md`).
Report: `~/.venvs/receivables-engine/bin/python scripts/build_reports.py`.

### Note for model owners (app reads these, by name)
Screens read the documented marts and these `results/*.json` keys: `prioritize.todays_queue`,
`prioritize.validation.held_out_metrics`, `cashflow.shortfall` (steady-state date),
`cashflow.shortfall_as_of_run_off`, `cashflow.backtest.value.{dates,summary.hybrid,totals_13w}`,
`cashflow.assumptions.cash_need_factor`, `credit.certificate_steady_state` / `certificate_as_of`,
`reserve.chain_ladder`, `drug.totals.MATURE`, `drug.losers_mature`, `econ.headline`. Interactive
recalculation calls `prioritize.choose`, `cashflow.forecast` / `cash_need` / `shortfall_metrics`,
`credit.eligibility` / `borrowing_base_table` / `assumptions`, `econ.appeal_threshold` /
`expected_net_recovery`. Renaming any of these breaks a screen; `tests/test_app.py` will catch it.

## From: documentation and career packaging — 2026-11-22

### R-DOC-1 LICENSE file (owner: core / repository owner)
README.md states "Code: MIT licence (see `LICENSE`)", but the repository has no `LICENSE` file. Please add
the standard MIT text with the copyright holder's name and year chosen by the owner.

### R-DOC-2 .gitignore entry for the dossier build folder (owner: core)
`scripts/build_dossier.py` writes LaTeX intermediates to `docs/project_dossier/build/` (the .tex, .log and
a copy of the PDF). Suggest adding `docs/project_dossier/build/` to `.gitignore`; the final PDF is
`docs/project_dossier/project_dossier.pdf`.

### R-DOC-3 App screenshots for the dossier (owner: app)
The dossier's app chapter reads screenshots if present: `figures/app_desk.png`, `figures/app_q1_cfo.png`,
`figures/app_q2_queue.png` (shown as figures) and `app_q3_cash.png`, `app_q4_reserve.png`,
`app_q5_borrowing_base.png`, `app_q6_drugs.png`, `app_q7_underpayment.png`, `app_methods.png` (listed).
After saving them, run `scripts/build_dossier.py`.

### Status from documentation (2026-11-24)
- R-DOC-3 withdrawn: after review the dossier no longer references app screenshots (no screenshots
  exist, none are invented). The app chapter lists the screens instead.
- `docs/consulting_memo.md` numbers are now under `<!--n:key-->` markers and refreshed by
  `scripts/build_docs_numbers.py --apply` (the memo path is in its `DOC_GLOBS`).
