"""Receivables reconciliation and risk engine: Streamlit app (offline, embedded PostgreSQL).

    python app/launch.py
    # or: ~/.venvs/receivables-engine/bin/streamlit run app/streamlit_app.py

One screen per decision question and job title; no overview screen. All data are simulated for a
fictional hospital. See app/README.md.
"""
from __future__ import annotations

import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import streamlit as st  # noqa: E402

from rre_app import state, ui  # noqa: E402
from rre_app.views import actuary, cfo, credit, desk, economist, methods, pharmacy, revcycle, treasurer  # noqa: E402

SCREENS = [
    (desk, "desk", ":material/fact_check:"),
    (cfo, "cfo", ":material/account_balance:"),
    (revcycle, "revenue-cycle", ":material/checklist:"),
    (treasurer, "treasurer", ":material/waterfall_chart:"),
    (actuary, "actuary", ":material/grid_on:"),
    (credit, "credit", ":material/credit_score:"),
    (pharmacy, "pharmacy", ":material/medication:"),
    (economist, "economist", ":material/balance:"),
    (methods, "methods", ":material/info:"),
]


def _page(mod, path, icon, default=False):
    return st.Page(mod.render, title=mod.TITLE, icon=icon, url_path=path, default=default)


def main() -> None:
    st.set_page_config(page_title="Receivables Reconciliation & Risk", page_icon=":material/fact_check:", layout="wide")
    ui.page_css()
    pages = {
        "Daily operations": [_page(desk, "desk", SCREENS[0][2], default=True)],
        "Decision questions": [_page(m, p, i) for m, p, i in SCREENS[1:-1]],
        "Reference": [_page(methods, "methods", SCREENS[-1][2])],
    }
    nav = st.navigation(pages, position="sidebar", expanded=True)
    with st.sidebar:
        st.markdown("<div class='rre-side'><b>Meridian Health System</b> (fictional)<br>"
                    "Simulated claims, remittances, bank and drug records. Every number comes from the pipeline "
                    "database and <code>results/</code>.</div>", unsafe_allow_html=True)
        s = state.startup()
        if s["ok"]:
            st.caption(f"Database: `{s['status'].database}`")
    nav.run()


main()
