# Runbook

## Environment

- Python 3.13 with the packages pinned in
  `requirements.txt`, installed into a virtual environment (`python3.13 -m venv .venv`, ignored by git;
  keep it outside the repository if the folder is cloud-synced).
- Embedded PostgreSQL 16 (`pixeltable-pgserver`), data directory outside the repository:
  `~/.cache/receivables-engine/pg`, override with `RRE_PG_DIR`. Set `DATABASE_URL` to use a hosted
  PostgreSQL instead.
- Tectonic (for this dossier only), found via `$TECTONIC` or `PATH`.

## Full rebuild

```
python run_all.py                  # generate, build, close, models, evaluate
python run_all.py --skip-generate  # reuse data/raw
python -m pytest                    # tests
python scripts/build_docs_numbers.py --apply
python scripts/build_dossier.py --refresh-schema
```

`run_all.py` generates the raw files with a fixed seed, builds the database, reconciles, closes
every day, evaluates the reconciliation against planted truth, runs the seven registered models,
runs `scripts/evaluate_models.py` and writes `results/` (every JSON file is written to a temporary
file and renamed, so a reader never sees a half-written file). The core database build and
daily close took <!--n:recon.build_seconds-->63<!--/n--> seconds; a full `run_all.py` with the seven models takes about
<!--n:repo.full_run_minutes-->5<!--/n--> minutes.

## Daily operation

1. New files land in `raw` (the CLI command `ingest` in `src/recon_engine/cli.py`, or the app's desk).
2. `stg.process_new()` and `core.process_new()` type and model only rows not yet processed.
3. `recon.run_all()` reruns matching for unmatched rows and rebuilds derived tables.
4. `call recon.close_day(d)` for each new business day in order. A refusal names the failing
   check; fix the data or the rule, rerun, and close again.
5. Work the exception queue by owner (section 5); refresh marts and models.

## When a control fails

| Symptom | First check |
|---|---|
| `RAW_ROWS_ACCOUNTED` fails | a raw file was loaded twice or truncated; compare `_file`, `_row_no` |
| `CASH_BANK_SIDE` fails | a deposit allocation was edited by hand; look at `recon.remit_bank_match` for the day |
| `close_day` refuses "statement does not chain" | a bank export overlaps or skips rows; check `bank_reference` repeats |
| `close_day` refuses "unmatched item missing from queue" | the exception build failed; rerun `recon.run_all()` |
| build suddenly slow | planner statistics missing after a rebuild; `ANALYZE` the recon tables (see `docs/sql_notes.md`) |

## Refreshing this dossier

After any rebuild: run `scripts/build_docs_numbers.py --apply` (refreshes every quoted number in
README, interview guide, one-pagers, CV bullets, practice notes and these sources), then
`scripts/build_dossier.py` (redraws diagrams, regenerates tables from `results/`, compiles the PDF).
`--check` on the numbers script exits non-zero if any document is stale.
