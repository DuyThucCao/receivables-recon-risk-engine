"""Q1 Hospital CFO: how much did we really collect, and where is cash stuck?"""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from .. import state, ui

TITLE = "Q1 Hospital CFO"
JOB = "Hospital CFO"
QUESTION = "How much did we really collect, and where is cash stuck?"

BUCKET_TEXT = {
    "NOT_YET_ADJUDICATED": "Not yet adjudicated", "PARTIAL_PAYMENT_BALANCE": "Partial-payment balance",
    "DENIED_IN_PLAY": "Denied, still appealable", "DENIED_WRITTEN_OFF": "Denied and written off",
    "UNDERPAID_VS_CONTRACT": "Underpaid vs contract", "CREDIT_BALANCE": "Credit balances (owed back)",
    "UNAPPLIED_CASH": "Unapplied cash in bank", "REMIT_NO_DEPOSIT": "Remitted, never deposited",
    "IN_TRANSIT": "In transit", "UNMATCHED_REMITTANCE": "Unmatched remittance",
    "PROVIDER_LEVEL_SUSPENSE": "Provider-level suspense", "ORPHAN_REVERSAL_LINES": "Orphan reversals (missing file)",
}


def render() -> None:
    ui.header(JOB, QUESTION, "Cash is counted three ways and tied to the cent: what the hospital billed, what payers "
              "said they paid on remittances, and what reached the bank. Everything below is a sum over reconciled "
              "simulated records, refreshed by `mart.refresh_q1()` after every intake on the desk.")
    ui.simulated_note()
    if not state.require_db():
        return
    d = state.load("cfo")
    if d["missing"]:
        ui.not_built(d["missing"])
        return
    L = "synthetic_record"
    t, ct = d["totals"], d["cash_totals"]
    names = d["payers"].set_index("payer_id")["payer_name"].to_dict()

    st.subheader("Collected versus billed")
    c = st.columns(5)
    ui.metric(c[0], "Billed charges", ui.usd_short(t["billed"]), L, help="Sum of claim line charges (list prices).")
    ui.metric(c[1], "Contract expected", ui.usd_short(t["contract_expected"]), L,
              help="What the contracts say payers owe on these lines (allowed amount), before denials.")
    ui.metric(c[2], "Payer paid (remittances)", ui.usd_short(t["payer_paid"]), L,
              delta=f"{ui.pct(t['payer_paid'] / t['contract_expected'])} of contract")
    ui.metric(c[3], "Payer cash in the bank", ui.usd_short(ct["payer_cash_deposited"]), L,
              help="ACH and lockbox payer credits on the bank statement.")
    ui.metric(c[4], "Unapplied cash", ui.usd_short(ct["unapplied_cash"]), L,
              help="Payer money in the bank that no remittance explains yet.")
    st.markdown(
        f"Of **{ui.usd(t['billed'])}** billed, contracts entitle the hospital to **{ui.usd(t['contract_expected'])}**. "
        f"Payers paid **{ui.usd(t['payer_paid'])}** and patients owe **{ui.usd(t['patient_responsibility'])}**; "
        f"**{ui.usd(t['denied'])}** of charges were denied and **{ui.usd(t['open_balance'])}** is still open. "
        "These five pieces plus the contractual adjustment add back to billed for every claim line.")

    st.subheader("Where the cash is stuck")
    s = d["cash_stuck"].copy()
    lost = s[s["bucket"] == "DENIED_WRITTEN_OFF"]
    s = s[s["bucket"] != "DENIED_WRITTEN_OFF"]
    s["name"] = s["bucket"].map(BUCKET_TEXT).fillna(s["bucket"])
    s = s.sort_values("amount")
    fig = go.Figure(go.Bar(x=s["amount"], y=s["name"], orientation="h",
                           marker_color=[ui.NEGATIVE if v < 0 else ui.ACCENT for v in s["amount"]],
                           customdata=s[["n_items", "description"]],
                           hovertemplate="%{y}<br>$%{x:,.2f}<br>%{customdata[0]:,} items<br>%{customdata[1]}<extra></extra>"))
    ui.chart(ui.style(fig, height=430, legend=False).update_xaxes(tickprefix="$", separatethousands=True)
             .update_yaxes(tickprefix=""), L,
             f"mart.cash_stuck as of {d['as_of']} (denied buckets valued at contract allowed)")
    if len(lost):
        st.markdown(f"Not shown: **{ui.usd(lost['amount'].iloc[0])}** of contract value on {int(lost['n_items'].iloc[0]):,} "
                    "denied lines whose claims are written off. That money is lost, not stuck.")
    top = d["cash_stuck"].sort_values("amount", ascending=False)
    in_play = top[top["bucket"].isin(["DENIED_IN_PLAY", "UNAPPLIED_CASH", "REMIT_NO_DEPOSIT", "PROVIDER_LEVEL_SUSPENSE",
                                      "NOT_YET_ADJUDICATED", "UNDERPAID_VS_CONTRACT", "PARTIAL_PAYMENT_BALANCE"])]
    st.markdown(
        "Recoverable or explainable money (not written off): " +
        "; ".join(f"**{BUCKET_TEXT.get(r.bucket, r.bucket).lower()}** {ui.usd(r.amount)} ({r.n_items:,} items)"
                  for r in in_play.itertuples()) + ".")
    with st.expander("Bucket definitions"):
        ui.table(d["cash_stuck"][["bucket", "description", "n_items", "amount", "billed_basis"]], L, "mart.cash_stuck",
                 money_cols=["amount", "billed_basis"])

    st.subheader("Accounts receivable aging")
    a = d["ar_aging"]
    fig = go.Figure()
    for p in sorted(a["payer_id"].unique()):
        g = a[a["payer_id"] == p]
        fig.add_bar(x=g["aging_bucket"], y=g["open_expected"], name=f"{p} {names.get(p, '')}",
                    marker_color=ui.PAYER_COLOR.get(p, ui.NEUTRAL),
                    hovertemplate=f"{p}<br>%{{x}} days<br>$%{{y:,.0f}}<extra></extra>")
    fig.update_layout(barmode="stack")
    ui.chart(ui.style(fig, y_title="Open, expected value", x_title="Days since submission"), L,
             "mart.ar_aging (open lines not written off; unadjudicated lines at contract allowed)")
    st.caption("The simulated hospital stopped billing after 2025-12-31, so at the as-of date the book is in run-off "
               "and most open money is old: a real going-concern hospital would show most A/R under 90 days.")

    st.subheader("P&L by payer and service line")
    tab1, tab2 = st.tabs(["By payer", "By service line"])
    cols = ["billed", "contractual_adjustment", "denied", "patient_responsibility", "payer_paid", "open_balance",
            "underpaid_vs_contract", "drug_cost_avg_basis", "margin_after_drug_cost"]
    with tab1:
        p = d["pnl_by_payer"].copy()
        p.insert(1, "payer_name", p["payer_id"].map(names))
        p["paid_share_of_billed"] = p["payer_paid"] / p["billed"]
        p["denied_share_of_billed"] = p["denied"] / p["billed"]
        ui.table(p[["payer_id", "payer_name", "n_lines"] + cols + ["paid_share_of_billed", "denied_share_of_billed"]], L,
                 "mart.pnl_by_payer_service (drug cost at average invoice cost, not FIFO)", money_cols=cols,
                 pct_cols=["paid_share_of_billed", "denied_share_of_billed"])
    with tab2:
        sv = d["pnl_by_service"].copy()
        sv["paid_share_of_billed"] = sv["payer_paid"] / sv["billed"]
        ui.table(sv[["service_line_code", "n_lines"] + cols + ["paid_share_of_billed"]], L,
                 "mart.pnl_by_payer_service", money_cols=cols, pct_cols=["paid_share_of_billed"])
        fig = go.Figure()
        sv = sv.sort_values("billed")
        for col, name, color in (("payer_paid", "Payer paid", ui.SERIES[0]), ("denied", "Denied", ui.SERIES[1]),
                                 ("open_balance", "Still open", ui.SERIES[2])):
            fig.add_bar(y=sv["service_line_code"], x=sv[col], name=name, orientation="h", marker_color=color,
                        hovertemplate="%{y}<br>" + name + ": $%{x:,.0f}<extra></extra>")
        fig.update_layout(barmode="group")
        ui.chart(ui.style(fig, height=380, money=False).update_xaxes(tickprefix="$", separatethousands=True), L,
                 "mart.pnl_by_payer_service")

    st.subheader("Monthly collections and the daily reconciliation")
    cp = d["cash_position"]
    fig = go.Figure()
    for col, name, color in (("billed_charges", "Billed charges", ui.NEUTRAL),
                             ("payer_paid_per_remittance", "Payer paid per remittance", ui.SERIES[0]),
                             ("payer_cash_deposited", "Payer cash deposited", ui.SERIES[2])):
        fig.add_scatter(x=cp["month"], y=cp[col], name=name, mode="lines", line=dict(width=2, color=color),
                        hovertemplate=name + "<br>%{x|%b %Y}: $%{y:,.0f}<extra></extra>")
    fig.update_layout(hovermode="x unified")
    ui.chart(ui.style(fig, y_title="Per month"), L, "mart.cash_position")
    dr = d["daily_recon"].copy()
    dr["recon_date"] = pd.to_datetime(dr["recon_date"])
    w = dr.set_index("recon_date")[["payer_credits_matched", "payer_credits_unapplied", "remit_unmatched"]].resample("W").sum()
    fig = go.Figure()
    for col, name, color in (("payer_credits_matched", "Payer credits matched to remittances", ui.SERIES[0]),
                             ("payer_credits_unapplied", "Payer credits unapplied", ui.SERIES[1]),
                             ("remit_unmatched", "Remittances with no deposit", ui.SERIES[7])):
        fig.add_bar(x=w.index, y=w[col], name=name, marker_color=color,
                    hovertemplate=name + "<br>week of %{x|%d %b %Y}: $%{y:,.0f}<extra></extra>")
    fig.update_layout(barmode="relative")
    ui.chart(ui.style(fig, y_title="Per week"), L, "recon.daily_recon (one row per calendar day, closed by recon.close_day)")
    n_closed = int((dr["status"] == "CLOSED").sum())
    st.caption(f"{n_closed:,} of {len(dr):,} days are CLOSED. A day closes only when the bank balance chains, every "
               "deposit is fully allocated, and payments dated that day equal same-day deposits plus timing differences "
               "plus unmatched remittances.")
