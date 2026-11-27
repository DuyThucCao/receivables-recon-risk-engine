%% \newpage
# Test evidence

`python -m pytest` collects <!--n:repo.tests-->183<!--/n--> tests in <!--n:repo.test_files-->17<!--/n--> files. They fall into
five groups.

| Group | What is proven | Examples |
|---|---|---|
| Tie-out | every control total passes to the cent; the identity holds on every line and in total against the raw files; posted money equals matched remittances; both cash identities; the statement chains | `test_core_tieout.py` |
| Close refusal | `close_day` refuses when one cent is corrupted in a bank amount, a deposit match, a claim balance or an allocation, or when days are closed out of order; the identity is a table constraint | `test_core_close_day.py` |
| Leakage and isolation | deleting future data does not change an earlier forecast, certificate, triangle, allocation or training set; pipeline code never mentions or opens the truth folder (static check and a runtime audit hook) | `test_core_pipeline_small.py`, each model's test file |
| Determinism | the same seed gives byte-identical raw files; models are deterministic for a seed | `test_core_determinism.py`, model tests |
| Hand-checked methods | 3-state Markov chain by hand; Kaplan-Meier by hand and against lifelines; 3x3 chain-ladder and Mack against the published benchmark; knapsack against enumeration; FIFO allocation by hand; break-even and deterrence formulas; borrowing-base waterfall and concentration fixed point | `test_markov.py`, `test_cashflow.py`, `test_reserve.py`, `test_prioritize.py`, `test_drug_fifo.py`, `test_econ_formulas.py`, `test_credit.py` |

Per file (generated from the test sources at build time):

@@table:tests@@

The most important tests are the ones that try to break the books on purpose. A reconciliation
that passes on clean data shows very little. One that refuses to close a day when a single cent
is moved shows that the controls actually work. The leakage tests do a narrower job for the
models: they check that changing records after the cutoff leaves the tested earlier outputs
unchanged.

The evaluation against planted truth is separate from the tests: `src/recon_engine/datagen/evaluate.py`
(reconciliation) and `scripts/evaluate_models.py` (models) are the only code that reads
`data/truth/`, and they write `results/reconciliation.json` and the `*_validation.json` /
`*_evaluation.json` files quoted throughout this dossier.

The practice SQL in `practice/` doubles as an external check: its solutions rebuild balances,
running bank balances, the daily reconciliation and FIFO allocations independently, and assert
that they agree with the stored tables (`practice/run_solutions.py`).
