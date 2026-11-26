"""Q7 Consulting economist: which payers underpay against contract, by how much, and why would they?"""
from __future__ import annotations

import plotly.graph_objects as go
import streamlit as st

from .. import loaders, state, ui

TITLE = "Q7 Consulting economist"
JOB = "Consulting economist"
QUESTION = "Which payers underpay against contract, by how much, and why would they?"


def render() -> None:
    ui.header(JOB, QUESTION, "Each priced claim line's first allowed amount is compared with its contract amount (in "
              "allowed terms, so deductibles and coinsurance never count as underpayment); intervals come from a "
              "bootstrap that resamples whole claims. Float is the interest a payer earns by paying late "
              "(`models/econ.py`).")
    ui.simulated_note()
    if not state.require_db():
        return
    d = state.load("economist")
    if "underpayment" not in d:
        ui.not_built(d["missing"])
        return
    u = d["underpayment"]
    names = d["payers"]
    res = d["results"] or {}
    hl = res.get("headline") or {}
    v = loaders.val
    k = st.columns(4)
    ui.metric(k[0], "Shortfall at first pricing", ui.usd_short(u["shortfall_usd"].sum()), "model_estimate")
    top = u.sort_values("shortfall_rate", ascending=False).iloc[0]
    ui.metric(k[1], "Highest shortfall rate", f"{top['payer_id']}: {ui.pct(top['shortfall_rate'], 2)}", "model_estimate",
              help=f"95% interval {ui.pct(top['shortfall_rate_lo'], 2)} to {ui.pct(top['shortfall_rate_hi'], 2)} of contract")
    if "float_value" in d:
        fv = d["float_value"]
        ui.metric(k[2], "Float beyond 30 days", ui.usd_short(fv["float_late_usd"].sum()), "model_estimate",
                  help="paid x SOFR (observed_public) x days beyond a 30-day window (financial_assumption) / 365")
    if hl:
        ui.metric(k[3], "Net recovery from appealing above threshold", ui.usd_short(v(hl.get("total_expected_net_recovery_usd"))),
                  "model_estimate")

    st.subheader("Underpayment by payer, with 95% intervals")
    uu = u.sort_values("payer_id")
    fig = go.Figure(go.Bar(x=[f"{p} {names.get(p, '')}" for p in uu["payer_id"]], y=uu["shortfall_usd"],
                           marker_color=[ui.PAYER_COLOR.get(p, ui.NEUTRAL) for p in uu["payer_id"]],
                           error_y=dict(type="data", symmetric=False, array=uu["shortfall_usd_hi"] - uu["shortfall_usd"],
                                        arrayminus=uu["shortfall_usd"] - uu["shortfall_usd_lo"], thickness=1.2, width=5),
                           customdata=uu[["shortfall_rate", "underpaid_claim_share"]],
                           hovertemplate="%{x}<br>shortfall $%{y:,.0f}<br>rate %{customdata[0]:.2%} of contract"
                                         "<br>%{customdata[1]:.1%} of priced claims underpaid<extra></extra>"))
    ui.chart(ui.style(fig, height=360, legend=False, y_title="Shortfall vs contract"), "model_estimate",
             "mart.underpayment (claim-clustered bootstrap, 2,000 replicates)")
    show = uu[["payer_id", "n_priced_claims", "n_underpaid_claims", "underpaid_claim_share", "underpaid_claim_share_lo",
               "underpaid_claim_share_hi", "shortfall_rate", "shortfall_rate_lo", "shortfall_rate_hi", "shortfall_usd",
               "shortfall_usd_lo", "shortfall_usd_hi", "post_payment_reduction_usd", "patient_resp_usd"]]
    ui.table(show, "model_estimate", "mart.underpayment",
             money_cols=["shortfall_usd", "shortfall_usd_lo", "shortfall_usd_hi", "post_payment_reduction_usd", "patient_resp_usd"],
             pct_cols=["underpaid_claim_share", "underpaid_claim_share_lo", "underpaid_claim_share_hi", "shortfall_rate",
                       "shortfall_rate_lo", "shortfall_rate_hi"])

    if "float_value" in d:
        st.subheader("Value of float: what paying late is worth to a payer")
        fv = d["float_value"].sort_values("payer_id")
        fig = go.Figure()
        fig.add_bar(x=fv["payer_id"], y=fv["float_late_first_pass_usd"], name="Late on first pass", marker_color=ui.SERIES[0])
        fig.add_bar(x=fv["payer_id"], y=fv["float_late_after_denial_usd"], name="Late because of a denial later paid",
                    marker_color=ui.SERIES[1])
        fig.update_layout(barmode="stack")
        ui.chart(ui.style(fig, height=330, y_title="Float beyond 30 days"), "model_estimate",
                 "mart.float_value (SOFR monthly mean: observed_public; 30-day window: financial_assumption)")
        st.markdown("For most payers the larger part of late float comes from claims paid only after a denial: on these "
                    "numbers a denial that is later overturned works as a delay.")

    if "econ_appeal_outcome" in d and "claims" in d:
        st.subheader("When is an underpayment worth appealing?")
        c1, c2 = st.columns([1, 2])
        cost = c1.number_input("Cost of one appeal (USD)", 1.0, 1000.0, d["appeal_cost_default"], 5.0,
                               help=d["appeal_cost_assumption"].get("source"))
        c1.markdown(ui.badge("financial_assumption"))
        pol = loaders.appeal_policy(d["claims"], d["econ_appeal_outcome"], cost)
        c1.markdown(f"Appeal a shortfall *s* when *s* x P(win) > cost, i.e. above **cost / P(win)**. "
                    f"At {ui.usd(cost, 0)} per appeal, appealing every claim above its payer's threshold is worth "
                    f"**{ui.usd(pol['expected_net_recovery_usd'].sum())}** net, on "
                    f"{int(pol['n_above_threshold'].sum()):,} claims.")
        with c2:
            fig = go.Figure()
            fig.add_bar(x=pol["payer_id"], y=pol["shortfall_above_usd"], name="Shortfall above threshold", marker_color=ui.SERIES[0])
            fig.add_bar(x=pol["payer_id"], y=pol["shortfall_below_usd"], name="Below threshold (not worth a fight)",
                        marker_color=ui.NEUTRAL)
            fig.update_layout(barmode="stack")
            ui.chart(ui.style(fig, height=320, y_title="Underpaid dollars"), "model_estimate",
                     "econ.appeal_threshold / econ.expected_net_recovery on mart.underpayment_claim; P(win) from denial "
                     "appeals (mart.econ_appeal_outcome, a stand-in)")
        ui.table(pol, "model_estimate", "recomputed for the appeal cost above",
                 money_cols=["threshold_usd", "shortfall_above_usd", "shortfall_below_usd", "expected_net_recovery_usd"],
                 pct_cols=["p_win"])

    if "underpayment_incentive" in d:
        st.subheader("Why would a payer underpay? The incentive arithmetic")
        inc = d["underpayment_incentive"]
        st.markdown("If being caught only means paying what was owed anyway, underpaying is a free option for the payer. "
                    "Underpaying stops paying only if the penalty is at least 1 / (q x P(win)) - 1 times the shortfall, where "
                    "q is the share appealed. A 12%-a-year interest clause over 90 days is about 3% of the shortfall, far "
                    "below that, so interest clauses deter delay but not underpricing. In a repeated game, the threat of "
                    "worse renewal terms can.")
        ui.table(inc[["payer_id", "p_win", "threshold_usd", "deterrence_penalty_multiple", "interest_clause_as_share_of_s_90d",
                      "payer_float_gain_per_dollar_30d_late", "annual_gain_shortfall_plus_late_float_usd",
                      "renewal_min_annual_loss_to_deter_usd"]], "model_estimate",
                 "mart.underpayment_incentive (12% clause and 0.9 renewal discount factor are financial_assumption)",
                 money_cols=["threshold_usd", "annual_gain_shortfall_plus_late_float_usd", "renewal_min_annual_loss_to_deter_usd"],
                 pct_cols=["p_win", "interest_clause_as_share_of_s_90d", "payer_float_gain_per_dollar_30d_late"])
    st.markdown("Limit: simulated underpayments are random draws, not strategic, so these data cannot show the behaviour "
                "the model describes (for example shortfalls bunched just below the appeal threshold). On real data that "
                "bunching would be the test. Nothing here is a finding about a real payer.")
