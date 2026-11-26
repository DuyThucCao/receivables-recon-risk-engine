%% \newpage
# Governance, data and limitations

## The five data labels

| Label | Meaning | Where it appears |
|---|---|---|
| `observed_public` | measured directly in a public source | CMS Transparency in Coverage issuer denial rates, p10 <!--n:public.cms_p10-->9.0%<!--/n--> to p90 <!--n:public.cms_p90-->24.5%<!--/n--> (<!--n:public.cms_year-->2025<!--/n--> data, <!--n:public.cms_n_issuers-->134<!--/n--> issuers, computed by this project from CMS data); SOFR <!--n:public.sofr-->3.91%<!--/n--> |
| `published_aggregate` | a statistic published by a public body | CMS Medicare Part B payment limits (<!--n:public.asp_quarter-->2026Q4<!--/n-->); KFF benchmark figures quoted for context |
| `synthetic_record` | simulated demonstration record | every claim, remittance, deposit, invoice and event |
| `financial_assumption` | an assumed business parameter | advance rate, age limit, spread, appeal cost, staff hours, prompt-pay window; the simulated payers' first-pass denial rates (<!--n:public.denial_rate_min-->11.1%<!--/n--> to <!--n:public.denial_rate_max-->23.3%<!--/n-->), chosen at CMS percentiles (calibrated assumption) |
| `model_estimate` | estimated by a model in this repository | transition matrix, reserve, borrowing base, overturn probability |

Every mart table carries its label (`mart.table_label` and a table comment), every results file
stamps each number, and every number in the documents is listed with its label in the appendix.
Public data sets are never joined at record level, because they share no valid key with the
simulated records or with each other.

## Public sources and their terms

Retrieved <!--n:public.retrieved-->2026-11-21<!--/n-->; URLs, byte sizes and SHA-256 hashes are in `data/public/manifest.json`, and
`docs/data_sources.md` states what each source can and cannot support.

- **CMS Transparency in Coverage Public Use File** (plan year 2027 file, 2025 data): issuer-level
  claims received and denied. CMS asks users to cite CMS, and altered or reprocessed data may not be
  presented as CMS data: the denial-rate percentiles here are **computed by this project from CMS
  data**. The method was checked by reproducing KFF's published figures for the prior year.
- **CMS Medicare Part B payment limit file**, October 2026: payment limit per HCPCS billing unit. A
  Medicare reimbursement reference, not a hospital's acquisition cost.
- **FRED**: SOFR ("Copyrighted: Citation Required"; cite Federal Reserve Bank of New York, retrieved
  from FRED, Federal Reserve Bank of St. Louis) and the 3-month Treasury yield (public domain,
  citation requested).
- **KFF** analyses quoted unaltered with attribution under CC BY-NC-ND 4.0.

## Ground-truth isolation

The generator writes the planted parameters and the true outcome of every record to
`data/truth/`. Pipeline and model code never open it: a static test checks the sources, and a
runtime test records every file opened during a small build under a Python audit hook. Models
receive a configuration (`model_config()`) that excludes generator behaviour. Only the evaluation
scripts read the truth, and they write separate files.

## How AI assistance was used

<!-- [Thuc: rewrite in your own words] --> The project was built with AI coding assistants working from a
written brief and a design document: the business flow, the documents, the seven questions, the
honesty rules and the acceptance criteria came first. The assistants wrote much of the code and
documentation. I ran the pipeline and the tests, read the SQL and the models, checked the results
against the answer key and the hand-worked examples, and can explain every step. <!-- [Thuc: adjust this paragraph to describe exactly what you did yourself.] -->

## Decisions that shaped the result

The decision log (`docs/decision_log.md`) records each design choice with its reason. The ones an
interviewer is most likely to ask about: integer cents and `numeric` money (D-001); one code path
for full and incremental runs (D-005); rules as data (D-006); unique candidate or nothing (D-007);
the contradiction guard added after evaluation found false matches (D-010); hard cases added after
a reviewer said perfect scores looked fitted (D-011); provider-level payments held in suspense
(D-012); models see only `model_config()` (D-013); integration with atomic results files (D-020).

## What simulated data can and cannot show

**Can:** that the pipeline ties to the cent; that the rules find the planted defects and refuse to
guess on ambiguous ones; that each model recovers the planted parameters; that each decision rule
behaves correctly (for example, the knapsack never picks a claim past its appeal window).

**Cannot:** how often real payers underpay; how accurate the rules would be on real 835 files; how
real appeal outcomes depend on claim features; whether any recommendation would save a real
hospital money. The generator and the rules were written by the same project, so matching scores
are an internal-consistency check.

## Limitations

- All transactional data are simulated; real remittances contain codes and edge cases this
  generator does not produce (secondary payers, corrected claims with new numbers, bundling).
- The overturn model is weak by construction of the data (AUC <!--n:q2.auc_model-->0.631<!--/n-->).
- The Markov chain is memoryless; the correction for time in state is simple.
- At the as-of date the simulated hospital is in run-off; cash and credit are evaluated at earlier
  dates as well.
- Chain-ladder overstated the true unpaid amount by <!--n:q4.cl_error-->30%<!--/n-->; per-month intervals undercover lumpy
  outcomes.
- Financial assumptions (advance rate, spread, age limit, appeal cost, staff hours, prompt-pay
  window, interest clause, renewal weight) are illustrative.
- No causal claim is made anywhere; the payer incentive analysis is reasoning, not an estimate.
- Backtests also guided model design, so they are not clean hold-outs: the cash model's lag
  weighting was changed after the first backtest over-forecast, and the credit PD switched to the
  dollar-weighted chain after the expected-loss backtest.
- Public inputs postdate the simulated as-of date (<!--n:recon.as_of_date-->2026-03-31<!--/n-->): SOFR is the observation of
  <!--n:public.sofr_as_of-->2026-11-19<!--/n--> and the drug reference prices are the <!--n:public.asp_quarter-->2026Q4<!--/n--> CMS limits. They calibrate the
  simulation; they are not what was known on the as-of date.
- The drug margin is before patient share (<!--n:q6.patient_resp-->$3,471,232.73<!--/n--> on drug lines treated as not collected);
  <!--n:q6.n_losing_if_patient_pays-->12<!--/n--> drugs would still lose money if all of it were collected.
- Some control totals hold by construction (the claim-line identity is also a CHECK constraint);
  the independent checks are the balance recompute, the bank statement chain, the allocation
  checks and the raw-to-core counts.
- Unapplied cash and "remitted, no deposit" mix true gaps with items the rules queued for manual
  matching (<!--n:recon.unapplied.true-->12<!--/n--> of <!--n:recon.unapplied.n-->64<!--/n--> and <!--n:recon.remit_no_deposit.true-->7<!--/n--> of <!--n:recon.remit_no_deposit.n-->51<!--/n--> are true gaps).
- Provider-level reconciliation and suggestions for ambiguous cash are open backlog items
  (`docs/backlog.md`).
