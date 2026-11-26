# Decision log

Each entry: date, decision, why, consequence. Newest last. Owner of this file for core entries: core.

**D-001 (2026-11-21) Integer cents in the generator, numeric(14,2) in the database.**
Every amount is generated as integer cents and written with exact formatting; the database uses
numeric(14,2) and never float (a test checks the schema). Rounding rules are identical on both sides
(half away from zero for percent-of-charge contracts). Consequence: identities hold exactly, so a
one-cent difference always means a real defect.

**D-002 (2026-11-21) Embedded PostgreSQL outside the repository.** `pixeltable-pgserver` with the data
directory at `~/.cache/receivables-engine/pg` (override `RRE_PG_DIR`); `DATABASE_URL` switches to a
hosted server. The repository path has spaces and is cloud-synced, which breaks Unix sockets and
makes the sync client churn (DESIGN.md section 4).

**D-003 (2026-11-21) Dependencies.** No new packages were installed. Business reasons: numpy/pandas
(data handling), scipy (root finding to calibrate logistic intercepts; statistics), psycopg +
pixeltable-pgserver (PostgreSQL, offline), PyYAML (calibration file), pytest (tests). statsmodels,
scikit-learn, lifelines, matplotlib, plotly, streamlit, reportlab are used by the analysis, app and
report workstreams and are pinned in requirements.txt for them.

**D-004 (2026-11-21) SQL files starting with "0" run before raw files are landed.** 00-03 create
schemas, raw tables, parse functions and rule/reference tables; then `pipeline.py` lands the raw
files; then 1x-3x stage, model, reconcile and build the question-1 marts. Model SQL lives in
`sql/models/` (integrator decision) and is not picked up by the core glob.

**D-005 (2026-11-21) One code path for full and incremental runs.** `stg.process_new()`,
`core.process_new()` and the matching functions only touch rows not yet processed, keyed on
(file, row number). Derived tables (payments, balances, status, controls, daily recon) are rebuilt
each run from core and the match tables, so they cannot drift. The app's intake helper calls the
same functions. Consequence: a match made earlier is kept even if later data would have made it
ambiguous (documented limitation).

**D-006 (2026-11-21) Rules are data, methods are functions.** `recon.match_rule` holds priority,
tolerance, window and an `active` flag; each `method` is a plpgsql function; the dispatcher refuses
an unknown method. Adding a rule with a different tolerance needs no code.

**D-007 (2026-11-21) Unique candidate or nothing.** No rule picks the "best" of several equally good
candidates (nearest date, first id). Ambiguous items go to the exception queue with reason
`AMBIGUOUS:`. Consequence: lower recall in the hard cases, no silent wrong postings.

**D-008 (2026-11-21) The planted Markov matrix is the visit-weighted population limit.** Denial and
overturn probabilities vary by claim (logistic in payer, service line, amount, reason category).
The pooled count estimator of a transition probability converges to
sum_i N_i[0,s] P_i[s,t] / sum_i N_i[0,s], with N_i the fundamental matrix of claim i. That matrix,
computed analytically, is what `planted_parameters.json` records per payer, next to the per-claim
mean. To keep the observed chain Markov, the denial reason is redrawn at every entry into DENIED
(including after an upheld appeal), and the hospital's next action depends only on that reason.

**D-009 (2026-11-21) Public calibration placement.** Payer first-pass denial rates are read off the
published issuer distribution at a fixed, assumed percentile per payer (linear interpolation
between p10/p25/median/p75/p90, clamped to p10-p90), not drawn at random, so payer differences stay
stable across seeds. The CMS Part B payment limits of 2026Q4 are applied to 2024-2025 service dates
as a stationary reference price (stated in the drug truth). SOFR replaces the assumed discount rate.
Each override is recorded in `data/raw/generation_metadata.json` and `results/core_build.json`.

**D-010 (2026-11-21) Contradiction guard in the identifier rules.** During development, evaluation
found 7 wrong CR10 matches: a transposed claim number equal to another real claim of the same day,
resolved on procedure + date + charge. A candidate is now ruled out when an echoed line control
number or member id disagrees. Those lines fall to CR30 and match correctly.

**D-011 (2026-11-21) Hard cases added after review.** The integrator pointed out that perfect scores
look like rules fitted to the generator. Added: legitimate repeated services (identical lines),
twin visits with a transposed claim number, split payments (second credit sometimes untraced) and
equal-amount provider-level payments without trace. Added rule RB25_TRACE_SPLIT; DUP10 became
claim-aware; CR10/CR20 pair identical sibling lines in order; CR30 uses an echoed line control
number as a tie-break. `recon.remit_bank_match` primary key became (trace_key, bank_txn_id) because
one payment can now map to several deposits (no column renamed). Remaining failures are reported per
case in `results/reconciliation.json`.

**D-012 (2026-11-21) Provider-level payments are not claim money.** Rows with claim status `PLB`
(periodic interim payments) are flagged `is_provider_level`, never matched to claims, and held as
`PROVIDER_LEVEL_PAYMENT` (INFO) so the posting split still ties. Reconciling them against claims is
out of scope.

**D-013 (2026-11-21) Models get `model_config()`, not the calibration.** `cfg` passed to
`run(conn, cfg)` contains the as-of date, seed, financial assumptions and discount rate with labels,
and deliberately excludes generator behaviour (denial coefficients, lags, underpayment rates) and the
override log (which contains the planted denial rates). Models must estimate those from the database.

**D-014 (2026-11-21) Ground-truth isolation is tested two ways.** Static: pipeline.py, db.py,
labels.py and sql/*.sql never contain the word; model modules and sql/models may discuss recovery in
comments but may not reference the truth folder, files or generator in code. Runtime: a small build
runs under a Python audit hook that records every file opened; the test requires the raw files to be
seen and nothing under the truth folder.

**D-015 (2026-11-21) Drug cost in the question-1 P&L uses average invoice cost.** `mart.pnl_by_payer_service`
uses units x average cost per billing unit over all invoices; FIFO lot costing belongs to the drug
module (`mart.drug_fifo`). The column is named `drug_cost_avg_basis` to make that visible.

**D-016 (2026-11-21) DESIGN.md clarifications (same edit).** Section 4: the "0" prefix rule, the
many-to-many shape of `recon.remit_bank_match`, provider-level rows, and that `results/` holds the
core outputs `core_build.json`, `control_totals.json`, `reconciliation.json`.

**D-017 (2026-11-21) Rejected-but-imputed dispense rows (R-DE-2).** An administration with unreadable
units stays in `stg.reject` (so reject counts and RAW_ROWS_ACCOUNTED are unchanged) and is also
carried into `core.fact_drug_dispense` with units imputed from its claim line and `units_imputed = true`,
so FIFO inventory can tie. Waste rows without a claim reference cannot be imputed.

**D-018 (2026-11-21) ORPHAN_REVERSAL as a line status (R-DE-3).** A reversal of a posting we never
received makes the balance misleading (open_balance = billed after payment). Such lines get status
`ORPHAN_REVERSAL` and are kept out of open A/R; the control identity still holds because the numbers
themselves are not altered.

**D-019 (2026-11-21) Explicit ANALYZE after rebuilds.** See docs/sql_notes.md: missing statistics plus
a per-row allocation subquery turned a 78 s build into 506 s.

**D-020 (2026-11-22) Integration: registry, evaluation step, atomic writes.** All seven models are
registered in `recon_engine.models.REGISTRY` (markov, cashflow, credit, prioritize, reserve, drug,
econ). `run_all.py` now runs the models and then `scripts/evaluate_models.py`, which compares the
outputs already in `results/` with the generator's planted parameters (it reads `data/truth/`, so it
stays outside the package); `--no-eval` skips it. Every JSON file under `results/` is written to a
temporary file and renamed, which removes the `JSONDecodeError` seen when two builds wrote
`core_build.json` at the same time (R-CCC-4). The four assumptions requested in R-CCC-2 are now in
`config/calibration.yaml` with `source_class: financial_assumption`; the modules' defaults stay as a
fallback.

**D-021 (2026-11-24) Publishing fixes after the independent review.** (1) `cli._jsonable` writes
any path inside the repository relative to its root, so `results/*.json` carry no machine-specific
absolute paths. (2) `data/public/` is now committed (about 2.6 MB of public CMS and FRED files):
without it the economics module falls back to a constant rate and a fresh clone would not reproduce
the published numbers. (3) `requirements.txt` adds `requests`, `openpyxl` and `pypdf`, which scripts
import; `pymupdf` stays optional. (4) Personal interview and CV preparation (`INTERVIEW_GUIDE.md`,
`docs/career/`) is kept locally and ignored by git, because it is coaching material for the author,
not documentation of the project.
