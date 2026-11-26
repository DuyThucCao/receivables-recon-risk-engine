"""Methods, data labels and limits."""
from __future__ import annotations

import re

import pandas as pd
import streamlit as st

from .. import state, ui

TITLE = "Methods, labels and limits"
JOB = "Anyone checking the work"
QUESTION = "Where does each number come from, and what can simulated data show?"

METHODS = [
    ("Reconciliation desk / Q1", "SQL rules as data (recon.match_rule), unique-candidate matching, exception queue, "
     "control identity per claim line, daily close procedure that refuses to close if totals do not tie",
     "sql/30-36, docs/reconciliation_rulebook.md"),
    ("Shared", "Absorbing Markov chain of the claim life cycle: N = (I - Q)^-1, B = N R, by payer", "models/markov.py"),
    ("Q2", "Logistic regression of appeal overturn, value = p x recoverable - cost, exact 0/1 knapsack, daily replay "
     "backtest against oldest-first and largest-first", "models/prioritize.py"),
    ("Q3", "Weighted Kaplan-Meier payment lag with right censoring, parametric tail, Monte Carlo, cash-at-risk and "
     "CVaR, interval-coverage backtest", "models/cashflow.py"),
    ("Q4", "Development triangle in SQL, chain-ladder, Mack standard error, ODP bootstrap, Bornhuetter-Ferguson, "
     "frequency-severity, retrospective test", "models/reserve.py"),
    ("Q5", "Net collectible value, eligibility waterfall, concentration cap (fixed point), advance rate, "
     "PD x LGD x EAD, rate and slower-payment stress", "models/credit.py"),
    ("Q6", "FIFO lot costing as a range-overlap SQL join with cent-exact allocation, margin decomposition", "models/drug.py"),
    ("Q7", "Shortfall vs contract with claim-clustered bootstrap, float = paid x rate x days / 365, break-even appeal "
     "threshold c / P(win), deterrence and repeated-game arithmetic", "models/econ.py"),
]


def _sources(md: str) -> pd.DataFrame:
    rows = []
    for block in re.split(r"\n## ", md)[1:]:
        title = block.splitlines()[0].strip()
        get = lambda key: next((m.group(1).strip() for m in [re.search(rf"\| {key} \| (.+?) \|\n", block)] if m), "")  # noqa: E731
        rows.append({"source": title, "publisher": get("Publisher"), "retrieved": get("Retrieved"), "label": get("Label")})
    return pd.DataFrame(rows)


def render() -> None:
    ui.header(JOB, QUESTION)
    if not state.require_db():
        return
    d = state.load("methods")
    st.subheader("The five data labels")
    st.markdown("Every table in `mart`, every chart and every number on these screens carries exactly one label.")
    for k, desc in d["labels"].items():
        st.markdown(f"- {ui.badge(k)} {desc}")
    st.markdown("Inputs that change assumptions (staff hours, cash need, advance rate, appeal cost) are labelled "
                "**financial_assumption** next to the control.")

    st.subheader("Label of every mart table")
    tl = pd.DataFrame(sorted(d["table_labels"].items()), columns=["table", "label"])
    st.dataframe(tl, hide_index=True, height=360)

    st.subheader("Public sources")
    if d["data_sources_md"]:
        src = _sources(d["data_sources_md"])
        st.dataframe(src, hide_index=True)
        st.caption("Full provenance, URLs, retrieval dates and terms of use: docs/data_sources.md. Public data sets are "
                   "never joined to the simulated records at record level.")

    st.subheader("Methods by screen")
    st.dataframe(pd.DataFrame(METHODS, columns=["screen", "method", "code"]), hide_index=True)

    st.subheader("What simulated data can and cannot show")
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Can show**\n"
                    "- that reconciliation ties to the cent and every unmatched dollar sits in a named bucket;\n"
                    "- that each method recovers the parameters planted in the generator (validation files in `results/`);\n"
                    "- that each decision rule behaves correctly (knapsack vs simple rules, appeal threshold, eligibility);\n"
                    "- that no model uses data from after its cutoff (tests delete the future and compare).")
    with c2:
        st.markdown("**Cannot show**\n"
                    "- anything about real payers, banks, hospitals or drug prices;\n"
                    "- causal effects (no identification strategy);\n"
                    "- real-world matching accuracy: the generator and the rules were written by the same project;\n"
                    "- strategic payer behaviour: simulated underpayments are random draws.")
    core = d.get("core") or {}
    rec = d.get("reconciliation") or {}
    if core:
        st.subheader("Build facts (pipeline database)")
        st.markdown(f"- As-of date {core.get('as_of_date')}; control totals all passed: **{core.get('control_totals_passed')}**; "
                    f"days closed {core.get('days_closed')} of {core.get('days_total')}.")
    if rec:
        cr = rec.get("claim_to_remittance", {}).get("overall", {})
        rb = rec.get("remittance_to_bank", {}).get("overall", {})
        st.markdown(f"- Against planted ground truth (internal consistency, not real-world accuracy): claim-to-remittance "
                    f"precision {cr.get('precision')}, recall {cr.get('recall_on_postable_lines')}; remittance-to-bank "
                    f"precision {rb.get('precision')}, recall {rb.get('recall')}.")
    st.caption("Built with AI assistance (see the README). Fictional hospital: Meridian "
               "Health System. Fictional payers P01-P06.")
