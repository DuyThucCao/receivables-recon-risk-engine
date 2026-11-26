"""Q5 Bank credit analyst: how much can we lend against these receivables?"""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from .. import loaders, state, ui

TITLE = "Q5 Bank credit analyst"
JOB = "Bank credit analyst"
QUESTION = "How much can we lend against these receivables?"


@st.cache_data(show_spinner=False, max_entries=8)
def _lines(cert: str, tok: str) -> pd.DataFrame:
    with state.connection() as c:
        return loaders.credit_lines(c, cert)


def render() -> None:
    ui.header(JOB, QUESTION, "An asset-based-lending borrowing base on the reconciled open receivables: each open line "
              "valued at net collectible value, an eligibility waterfall, a payer concentration cap, an advance rate, "
              "expected loss from the Markov chain, and an interest-rate stress (`models/credit.py`).")
    ui.simulated_note()
    if not state.require_db():
        return
    d = state.load("credit")
    if "base" not in d:
        ui.not_built(d["missing"])
        return
    A = d["assumptions"]
    dates = d["certificate_dates"]
    default = "2025-09-30" if "2025-09-30" in dates else dates[0]
    st.markdown(f"**Certificate and lending terms** {ui.badge('financial_assumption')}")
    c = st.columns(5)
    cert = c[0].selectbox("Certificate date", dates, index=dates.index(default),
                          format_func=lambda x: x + (" (still billing)" if x != dates[-1] else " (as-of, run-off)"))
    adv = c[1].slider("Advance rate", 0.50, 0.95, float(A["advance_rate"]["value"]), 0.01, help=A["advance_rate"]["source"])
    lim = c[2].slider("Concentration limit per payer", 0.10, 1.00, float(A["concentration_limit"]["value"]), 0.05,
                      help=A["concentration_limit"]["source"])
    age = c[3].select_slider("Max age (days since submission)", [60, 90, 120, 150, 180, 270, 365],
                             int(A["eligibility_max_age_days"]["value"]), help=A["eligibility_max_age_days"]["source"])
    cross = c[4].slider("Cross-age trigger", 0.10, 1.00, float(A["cross_age_pct"]["value"]), 0.05,
                        help=A["cross_age_pct"]["source"])
    if cert == dates[-1]:
        st.info("At the as-of date the simulated hospital has billed nothing for three months, so almost every open "
                "line is past the age limit and availability is near zero. The 2025-09-30 certificate shows the "
                "facility while the hospital is still billing.")

    lines = _lines(cert, state.token())
    base = d["base"][d["base"]["certificate_date"].astype(str) == cert]
    t = loaders.recompute_borrowing_base(lines, base, adv, lim, age, cross)
    tot = t.sum(numeric_only=True)
    k = st.columns(4)
    ui.metric(k[0], "Gross net collectible value", ui.usd_short(tot["gross_ncv"]), "model_estimate")
    ui.metric(k[1], "Eligible after cap", ui.usd_short(tot["eligible"]), "model_estimate")
    ui.metric(k[2], "Availability (can lend)", ui.usd_short(tot["availability"]), "model_estimate")
    margin = (1 - adv) * tot["eligible"]
    ui.metric(k[3], "Expected loss vs lender margin", f"{ui.usd_short(tot['expected_loss'])} / {ui.usd_short(margin)}",
              "model_estimate", help="EL = PD x LGD x EAD per payer; margin = (1 - advance rate) x eligible.")

    if tot["expected_loss"] > margin:
        st.warning(f"Expected loss ({ui.usd(tot['expected_loss'])}) is larger than the lender's margin "
                   f"({ui.usd(margin)} = (1 - advance rate) x eligible): at these terms the haircut does not cover the "
                   "expected write-offs, so a lender would lower the advance rate or tighten eligibility.")
    st.subheader("From gross receivables to availability")
    steps = [("Gross NCV", tot["gross_ncv"], "absolute"), ("Denied", -tot["inel_denied"], "relative"),
             ("Disputed", -tot["inel_disputed"], "relative"), (f"Over {age} days", -tot["inel_aged"], "relative"),
             ("Cross-aged", -tot["inel_cross_aged"], "relative"), ("Over cap", -tot["concentration_excess"], "relative"),
             ("Eligible", tot["eligible"], "total"), (f"Haircut {1 - adv:.0%}", -(1 - adv) * tot["eligible"], "relative"),
             ("Credit bal.", -tot["credit_balance_reserve"], "relative"), ("Available", tot["availability"], "total")]
    fig = go.Figure(go.Waterfall(x=[s[0] for s in steps], y=[s[1] for s in steps], measure=[s[2] for s in steps],
                                 connector=dict(line=dict(width=1, color=ui.NEUTRAL)),
                                 increasing=dict(marker_color=ui.SERIES[0]), decreasing=dict(marker_color=ui.SERIES[1]),
                                 totals=dict(marker_color=ui.SERIES[0]),
                                 hovertemplate="%{x}: $%{y:,.0f}<extra></extra>"))
    ui.chart(ui.style(fig, height=420, legend=False), "model_estimate",
             f"credit.eligibility + credit.borrowing_base_table on mart.borrowing_base_line at {cert}; inputs above are "
             "financial_assumption")

    st.subheader("Expected loss by payer")
    names = d["payers"]
    t2 = t.copy()
    t2.insert(1, "payer", t2["payer_id"].map(names))
    t2["el_rate"] = t2["expected_loss"] / t2["ead"].where(t2["ead"] > 0)
    ui.table(t2[["payer_id", "payer", "gross_ncv", "inel_denied", "inel_disputed", "inel_aged", "inel_cross_aged",
                 "eligible_pre_cap", "concentration_excess", "eligible", "availability", "pd", "lgd", "expected_loss", "el_rate"]],
             "model_estimate", "PD (Markov write-off probability) and LGD from mart.borrowing_base; EAD = recomputed eligible",
             money_cols=["gross_ncv", "inel_denied", "inel_disputed", "inel_aged", "inel_cross_aged", "eligible_pre_cap",
                         "concentration_excess", "eligible", "availability", "expected_loss"], pct_cols=["pd", "lgd", "el_rate"])
    st.caption("Payer names are fictional. Concentration: no payer may exceed the limit of the final eligible base "
               "(E = sum of min(e_p, limit x E), solved by iteration).")

    if "stress" in d:
        st.subheader("Interest-rate and slower-payment stress")
        s = d["stress"][d["stress"]["certificate_date"].astype(str) == cert].copy()
        drawn = max(float(tot["availability"]), 0.0)
        s["drawn_at_your_terms"] = [drawn if "slower" not in x else None for x in s["scenario"]]
        s["interest_13w_at_your_terms"] = s["rate_annual"] * s["drawn_at_your_terms"].astype(float) * 91 / 360
        fig = go.Figure()
        fig.add_bar(x=s["scenario"], y=s["interest_cost_13w"], name="Interest, 13 weeks (model terms)", marker_color=ui.SERIES[0])
        fig.add_bar(x=s["scenario"], y=s["carry_cost_of_delay"], name="Financing cost of waiting for collections",
                    marker_color=ui.SERIES[2])
        fig.update_layout(barmode="group")
        ui.chart(ui.style(fig, height=340, y_title="Cost"), "model_estimate",
                 "mart.borrowing_base_stress (rate = SOFR observed_public + spread financial_assumption, ACT/360)")
        ui.table(s[["scenario", "rate_annual", "availability", "drawn", "interest_cost_13w", "carry_cost_of_delay",
                    "drawn_at_your_terms", "interest_13w_at_your_terms"]], "model_estimate",
                 "mart.borrowing_base_stress; the last two columns apply the same formula to the availability at your terms "
                 "(slower-payment rows need the model rerun, left blank)",
                 money_cols=["availability", "drawn", "interest_cost_13w", "carry_cost_of_delay", "drawn_at_your_terms",
                             "interest_13w_at_your_terms"], pct_cols=["rate_annual"])
        st.caption(f"SOFR {ui.pct(A['sofr']['value'], 2)} ({A['sofr']['label']}); spread "
                   f"{ui.pct(A['facility_spread']['value'], 2)} ({A['facility_spread']['label']}).")
