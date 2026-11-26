"""Q6 Pharmacy manager: which drugs lose money after reimbursement?"""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from .. import state, ui

TITLE = "Q6 Pharmacy manager"
JOB = "Pharmacy manager"
QUESTION = "Which drugs lose money after reimbursement?"

PARTS = [("price_gap", "Contract minus FIFO cost", +1), ("denied_leak", "Denied claims", -1),
         ("patient_resp", "Patient share (not collected)", -1), ("pending_leak", "Payment pending", -1),
         ("pricing_gap", "Paid below contract", -1), ("waste_cost", "Expired waste", -1)]


def render() -> None:
    ui.header(JOB, QUESTION, "Every administration is costed with FIFO lot costing written as a set-based SQL join "
              "(`sql/models/61_drug_fifo.sql`), and margin per drug is reconciled payer cash minus FIFO cost minus "
              "expired waste, split exactly into where the money went (`models/drug.py`).")
    ui.simulated_note()
    if not state.require_db():
        return
    d = state.load("pharmacy")
    if "margin" not in d:
        ui.not_built(d["missing"])
        return
    m = d["margin"]
    cohort = st.radio("Cohort", ["MATURE", "ALL"], horizontal=True,
                      help="MATURE: services at least 180 days before the as-of date, so most claims have settled "
                           "(the 180 days is an assumption). ALL includes recent services still in process.")
    m = m[m["cohort"] == cohort].copy()
    tot = m[["payer_paid", "fifo_cogs", "waste_cost", "margin_collected", "price_gap", "denied_leak"]].sum()
    k = st.columns(4)
    ui.metric(k[0], "Drugs losing money", f"{int((m['margin_collected'] < 0).sum())} of {len(m)}", "model_estimate")
    ui.metric(k[1], "Margin after reimbursement", ui.usd_short(tot["margin_collected"]), "model_estimate")
    ui.metric(k[2], "Contract minus FIFO cost", ui.usd_short(tot["price_gap"]), "model_estimate",
              help="What the contracts would pay above what the drugs cost.")
    ui.metric(k[3], "Lost to denied drug claims", ui.usd_short(tot["denied_leak"]), "model_estimate")
    st.markdown(f"Contracts would pay **{ui.usd(tot['price_gap'])}** more than the drugs cost, so acquisition price is "
                f"not what loses money. Denied drug claims remove **{ui.usd(tot['denied_leak'])}** of contract value. "
                "In the generator, denial odds rise with claim amount, so the most expensive drugs are denied most often.")

    st.subheader("Margin ranking and its decomposition")
    m = m.sort_values("margin_collected")
    fig = go.Figure()
    for i, (col, name, sign) in enumerate(PARTS):
        fig.add_bar(y=m["hcpcs"], x=sign * m[col], name=name, orientation="h", marker_color=ui.SERIES[i],
                    customdata=m["short_description"],
                    hovertemplate="%{y} %{customdata}<br>" + name + ": $%{x:,.0f}<extra></extra>")
    fig.add_scatter(y=m["hcpcs"], x=m["margin_collected"], mode="markers", name="Margin (sum of the parts)",
                    marker=dict(size=9, color="rgba(0,0,0,0)", line=dict(width=2, color=ui.NEUTRAL), symbol="diamond"),
                    hovertemplate="%{y}: margin $%{x:,.0f}<extra></extra>")
    fig.update_layout(barmode="relative")
    fig.update_yaxes(categoryorder="array", categoryarray=list(m["hcpcs"])[::-1])  # biggest loss on top
    ui.chart(ui.style(fig, height=max(420, 22 * len(m)), money=False).update_xaxes(tickprefix="$", separatethousands=True),
             "model_estimate", f"mart.drug_margin, cohort {cohort}: margin = (contract - FIFO cost) - waste - patient share "
             "- denied - pending - paid below contract")
    show = m[["hcpcs", "short_description", "n_claim_lines", "payer_paid", "fifo_cogs", "waste_cost", "margin_collected",
              "margin_if_patient_pays", "denied_line_share", "primary_driver", "primary_driver_amount", "cost_to_asp"]]
    ui.table(show, "model_estimate", "mart.drug_margin (ASP payment limit: published_aggregate, a Medicare reference, not cost)",
             money_cols=["payer_paid", "fifo_cogs", "waste_cost", "margin_collected", "margin_if_patient_pays",
                         "primary_driver_amount"], pct_cols=["denied_line_share"])

    if "lots" in d:
        st.subheader("FIFO lot position")
        lots = d["lots"]
        ok_u, ok_v = bool(lots["units_identity_ok"].all()), bool(lots["value_identity_ok"].all())
        lt = lots[["purchased_value", "administered_cost", "wasted_cost", "on_hand_value"]].sum()
        k = st.columns(4)
        ui.metric(k[0], "Lots", f"{len(lots):,}", "synthetic_record")
        ui.metric(k[1], "Purchased", ui.usd_short(lt["purchased_value"]), "synthetic_record")
        ui.metric(k[2], "On hand at FIFO cost", ui.usd_short(lt["on_hand_value"]), "synthetic_record")
        ui.metric(k[3], "Lot identity", "ties on every lot" if ok_u and ok_v else "BREAK", "synthetic_record",
                  help="purchased = administered + wasted + on hand, in units and to the cent")
        drug = st.selectbox("Drug", sorted(lots["hcpcs"].unique()),
                            index=sorted(lots["hcpcs"].unique()).index(m.iloc[0]["hcpcs"]) if len(m) else 0,
                            format_func=lambda h: f"{h} {d['margin'].set_index('hcpcs')['short_description'].to_dict().get(h, '')}")
        g = lots[lots["hcpcs"] == drug].copy()
        fig = go.Figure()
        for col, name, color in (("administered_units", "Administered", ui.SERIES[0]), ("wasted_units", "Expired (waste)", ui.SERIES[1]),
                                 ("on_hand_units", "On hand", ui.SERIES[2])):
            fig.add_bar(x=g["lot_no"], y=g[col], name=name, marker_color=color,
                        hovertemplate="lot %{x}<br>" + name + ": %{y:,} units<extra></extra>")
        fig.update_layout(barmode="stack")
        fig.update_xaxes(type="category")
        ui.chart(ui.style(fig, height=340, money=False, y_title="Billing units", x_title="Lot, in receipt order"),
                 "synthetic_record", "mart.drug_lot_position")
        ui.table(g[["lot_no", "invoice_no", "received_date", "expiry_date", "purchased_units", "administered_units",
                    "wasted_units", "on_hand_units", "purchased_value", "administered_cost", "wasted_cost", "on_hand_value",
                    "expired_still_on_hand", "infeasible_units"]], "synthetic_record", "mart.drug_lot_position",
                 money_cols=["purchased_value", "administered_cost", "wasted_cost", "on_hand_value"])
        st.caption("`infeasible_units`: units FIFO places on a lot after its expiry date (the pharmacist really used a "
                   "fresher lot); flagged, not hidden.")
    st.markdown("Limits: simulated purchase prices are a fixed ratio to the CMS payment limit, so the cost-versus-ASP "
                "picture is built in, not discovered; patient share is treated as not collected (the other bound is "
                "`margin_if_patient_pays`); no partial-vial waste records exist.")
