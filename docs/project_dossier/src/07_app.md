%% \newpage
# App walkthrough

The Streamlit app (`app/`) runs fully offline against the embedded database. Each screen serves
one question and one role; there is no generic overview screen. Everything shown comes from the
pipeline: the marts and the JSON files in `results/`. The reconciliation desk writes to a separate
demo copy of the database that can be reset, so a demonstration never changes the reference build.

Start it with: <!--n:repo.app_command-->python app/launch.py<!--/n-->.

This dossier contains no screenshots of the app. To show the app, run it and walk through the
screens listed below.

## Screen 1: reconciliation desk (cash-posting analyst)

Enter one claim, or upload a remittance batch and a bank statement in the raw export formats. The
desk lands the rows, runs the same incremental functions as the nightly build
(`stg.process_new()`, `core.process_new()`, `recon.run_all()`) and shows at once which lines matched
which claim lines and by which rule, which deposits matched which payments, what was rejected in
staging and which new exceptions were opened. The numbers on the other screens move accordingly.

The reconciliation desk is the quickest demonstration of the engine. Upload a small remittance
file with one duplicate line, one mangled claim number and one payment whose deposit is missing.
The app excludes the duplicate, matches the claim with the normalised rule, leaves the payment in
"remitted, no deposit", and the control totals still tie.

## Screens 2 to 8: one per decision question

The screen list follows the design (`docs/DESIGN.md`, section 7); check each description against
the final app before sharing this document.

| Screen | Role | What it shows |
|---|---|---|
| Cash position and where cash is stuck | CFO | collected vs billed by month, cash-stuck buckets, aging, P&L by payer and service line |
| Denial work queue | revenue-cycle lead | today's ranked list with a plain-English reason per claim, policy comparison |
| 13-week cash forecast | treasurer | weekly fan chart, cash-at-risk, shortfall probability under an adjustable cash need |
| Unpaid claims reserve | payer actuary | development triangle, chain-ladder and BF estimates, bootstrap distribution |
| Borrowing base | bank credit analyst | certificate waterfall, expected loss vs margin, rate stress |
| Drug margin | pharmacy manager | margin per drug and its decomposition, FIFO lots |
| Payer underpayment | consulting economist | shortfall by payer with intervals, appeal thresholds, float |
| Methods, labels and limits | everyone | the five data labels, sources, assumptions, what simulated data cannot show |

## The CFO report

A PDF report for the CFO is built from the question-1 marts and the model results (path:
<!--n:repo.report_pdf-->reports/cfo_report.pdf<!--/n-->). It is written as a narrative for a decision maker, not
as chart captions, and every figure in it carries its data label.
