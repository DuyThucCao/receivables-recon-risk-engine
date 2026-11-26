%% \begin{titlepage}\centering
%% \vspace*{3cm}
%% {\Huge\bfseries Receivables Reconciliation \& Risk Engine\par}
%% \vspace{0.6cm}
%% {\Large Project dossier\par}
%% \vspace{0.4cm}
%% {\large Three-way payment reconciliation to the cent, and seven decision questions on the reconciled receivables\par}
%% \vspace{1.2cm}
%% {\large Thuc Cao\par}
%% {\normalsize B.S. Data Science, minor in Economics and Statistics, Rutgers University--New Brunswick, 2026\par}
%% \vspace{1.6cm}
%% \begin{minipage}{0.8\linewidth}\small
%% All transactional data in this dossier are \textbf{simulated} for a fictional hospital (``Meridian Health System''), six fictional payers, one bank account and one drug wholesaler. Only labelled calibration figures come from public sources. Results show that the methods work and recover what the simulator planted; they are not findings about any real organisation. Every number is generated from \texttt{results/*.json} through \texttt{docs/\_numbers.md}. \ifdraft Paragraphs marked \thuctag{} are drafts the author must rewrite in his own words before using this document.\fi
%% \end{minipage}
%% \vfill
%% {\small Built with AI assistance. The author reviewed and ran the code and can explain every step.\par}
%% \end{titlepage}
%% \setcounter{tocdepth}{1}\tableofcontents
%% \newpage

# Proof of work {-}

This page answers four questions: what the real problem is, why this solution addresses it,
which logic and techniques it uses, and what the numbers are worth. Dollar amounts are results
**inside the simulation**.

**1. What is the problem?** A business that is owed money is paid late, in pieces and by several
parties. A hospital bills an insurer; the insurer sends a remittance advice that pays, adjusts or
denies each line, groups payments under a trace number, and sends money to the bank as lump sums
that rarely line up one to one with the paperwork. Lines are duplicated, reversed, reposted,
recouped; claim numbers come back mangled; deposits arrive with no remittance; remittances
arrive with no deposit. Unless each link is reconciled, cash is misstated and nobody can say
where it is stuck. The same chain exists in any business with receivables, which is why banks
employ settlement and reconciliation teams.

**2. Why does this solution address it?** It follows every receivable through the three
documents that should agree (the claim, the payer's remittance, the bank deposit) and ties them
to the cent with explicit identities. Anything that does not match lands in an exception queue
with a reason and an owner, instead of being guessed. A daily close procedure refuses to close a
day that is off by one cent. Only on top of that reconciled base does the project answer seven
decision questions asked by named roles: CFO, revenue-cycle lead, treasurer, payer actuary, bank
credit analyst, pharmacy manager and consulting economist.

**3. Which logic and techniques?** SQL first: a layered PostgreSQL model (raw, staging, core,
reconciliation, marts), matching rules stored as data, a claim-line state machine, control totals
and a close procedure. On top: an absorbing Markov chain of the claim life cycle (fundamental
matrix), survival analysis with right censoring and Monte Carlo for the cash forecast, a
borrowing-base certificate with expected loss, a logistic model and an exact knapsack for the
denial queue, chain-ladder with Mack and bootstrap intervals for the reserve, FIFO lot costing in
SQL for drugs, and a claim-level bootstrap plus simple game theory for payer underpayment.

**4. What are the numbers worth (simulated)?** The reconciliation ties <!--n:recon.control_checks-->24<!--/n--> control totals with a
largest difference of <!--n:recon.control_max_diff-->$0.00<!--/n--> and closes <!--n:recon.days_closed-->820<!--/n--> of <!--n:recon.days_total-->820<!--/n--> days. In the simulated books it
finds <!--n:recon.unapplied.amount-->$2,048,321.11<!--/n--> of payer credits not yet applied (<!--n:recon.unapplied.true-->12<!--/n--> of <!--n:recon.unapplied.n-->64<!--/n--> items are true
deposits without a remittance; the rest are queued for manual matching), <!--n:recon.remit_no_deposit.amount-->$1,596,219.08<!--/n--> remitted
with no matched deposit (<!--n:recon.remit_no_deposit.true-->7<!--/n--> of <!--n:recon.remit_no_deposit.n-->51<!--/n--> payments truly missing) and <!--n:recon.denied_in_play.amount-->$2,292,185.59<!--/n--> of denials
still workable. The denial queue for one day is worth <!--n:q2.today_value-->$324,591<!--/n--> in expected net recovery,
against <!--n:q2.today_largest-->$311,468<!--/n--> for a simple largest-first rule and <!--n:q2.today_oldest-->$30,948<!--/n--> for oldest-first: the model's gain over
largest-first is small. Payer underpayment totals <!--n:q7.shortfall-->$334,486.29<!--/n-->, with
expected net recovery of <!--n:q7.net_recovery-->$104,252.32<!--/n--> after appeal costs. A lender could advance <!--n:credit.availability-->$2,656,726<!--/n--> on the
steady-state certificate, but expected loss (<!--n:credit.expected_loss-->$662,340<!--/n-->) exceeds the lender's margin. These are
simulation results: they show what the engine can measure, not money saved anywhere.

## Summary in ten sentences (for a non-specialist) {-}

1. When a hospital, or any business, sends a bill, the money comes back later, in pieces, from several parties, and some of it gets lost on the way.
2. This project is a working tool that follows each bill from the invoice, to the payer's payment advice, to the deposit in the bank, and checks that every cent is accounted for.
3. Healthcare is the example because a medical bill passes through a hospital, an insurer, a bank and a drug supplier, but the method works for any company with customers who owe it money.
4. Real patient and bank records are private, so a simulator produces realistic fake documents for a fictional hospital, with the errors real files contain.
5. The tool matched <!--n:recon.c2r.matched-->88,467<!--/n--> payment lines to bills with no wrong matches against the simulator's answer key (on errors the simulator planted itself), and its <!--n:recon.control_checks-->24<!--/n--> accounting checks balance to the cent.
6. When it is not sure, it does not guess; it puts the item on a list for a person.
7. On top of the reconciled data it answers seven practical questions, such as which denied claims to work first, how much a bank could lend, and which drugs lose money.
8. Each answer is tested against the simulator's known truth, and weak methods are reported as weak.
9. It is written in SQL and Python, with <!--n:repo.tests-->183<!--/n--> automated tests and a small app.
10. It was built with AI assistance; the author ran and checked every step and can explain it.

## How to read this dossier {-}

Sections 1 to 3 describe the business, the documents and the data model. Sections 4 and 5 are
the reconciliation rulebook and the control framework, the core of the project. Section 6 covers
the shared Markov chain and the seven decision questions, each with the question, data, method,
validation and limits. Sections 7 to 10 cover the app, test evidence, the runbook and governance.
A glossary and the full table of quoted numbers close the document.

Every number carries one of five labels: `observed_public`, `published_aggregate`,
`synthetic_record`, `financial_assumption` or `model_estimate`. The appendix lists each number with
its label and source file.
