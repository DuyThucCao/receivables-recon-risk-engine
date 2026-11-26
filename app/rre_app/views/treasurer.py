"""Q3 Treasurer: will we be short of cash in the next 13 weeks?"""
from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from .. import loaders, state, ui

TITLE = "Q3 Treasurer"
JOB = "Treasurer"
QUESTION = "Will we be short of cash in the next 13 weeks?"


@st.cache_data(show_spinner="Running the Monte Carlo (2,000 paths) ...", max_entries=16)
def _simulate(forecast_date: str, tok: str) -> dict:
    with state.connection() as c:
        return loaders.simulate_receipts(c, forecast_date)


def _band(fig: go.Figure, x, lo, hi, name: str, opacity: float) -> None:
    fig.add_scatter(x=x, y=hi, mode="lines", line=dict(width=0), showlegend=False, hoverinfo="skip")
    fig.add_scatter(x=x, y=lo, mode="lines", line=dict(width=0), fill="tonexty", name=name,
                    fillcolor=f"rgba(42,120,214,{opacity})", hoverinfo="skip")


def render() -> None:
    ui.header(JOB, QUESTION, "Weekly payer receipts are simulated claim by claim: whether each open claim will be paid "
              "comes from the Markov chain of the claim life cycle, when from a censored payment-lag curve "
              "(weighted Kaplan-Meier with a parametric tail), plus the bank's deposit lag and new claims arriving "
              "as a Poisson process (`models/cashflow.py`).")
    ui.simulated_note()
    if not state.require_db():
        return
    d = state.load("treasurer")
    res = d["results"] or {}
    if "forecast" not in d:
        ui.not_built(d["missing"])
        return
    A = d["assumptions"]
    asof = str(d["forecast"]["forecast_date"].max())
    ss = loaders.steady_state_date()
    bt_dates = ((res.get("backtest") or {}).get("value") or {}).get("dates") or []
    options = [ss, asof] + [x for x in reversed(bt_dates) if x not in (ss, asof)]
    label_of = {ss: f"{ss} (still billing: the model's headline)", asof: f"{asof} (as-of date, run-off book)"}
    c1, c2, c3 = st.columns([1.3, 1, 1])
    f = c1.selectbox("Forecast date", options, index=0, format_func=lambda x: label_of.get(x, f"{x} (backtest date)"),
                     help="Every input is cut at the forecast date, so a past date shows what the model would have "
                          "said then, and the bank's actual receipts are drawn on top.")
    factor = c2.slider("Weekly cash need, x median weekly receipts", 0.70, 1.30,
                       float(A["cash_need_factor"]["value"]), 0.05, help=A["cash_need_factor"]["source"])
    min_bal = c3.number_input("Minimum operating balance (USD)", 0.0, 2e7, float(A["minimum_operating_balance_usd"]["value"]),
                              100_000.0, help=A["minimum_operating_balance_usd"]["source"])
    c3.markdown(ui.badge("financial_assumption"))

    sim = _simulate(f, state.token())
    need = factor * sim["median_receipts"]
    c2.markdown(f"= **{ui.usd(need)}** a week &nbsp; {ui.badge('financial_assumption')}  \n"
                f"(median of the 26 weeks before {f}: {ui.usd(sim['median_receipts'])}, synthetic_record)")
    sm = loaders.shortfall(sim, need, min_bal)
    q = sim["quantiles"]
    K = len(q["mean"])
    weeks = np.arange(1, K + 1)
    if sim["run_off"]:
        st.info("After 2025-12-31 the simulated hospital bills nothing, so this is a run-off of the open book, not a "
                "going-concern forecast: receipts fall towards zero and any need anchored to past receipts falls short. "
                "The steady-state date shows the model while the hospital is still billing, with actuals.")

    k = st.columns(5)
    ui.metric(k[0], "P(shortfall in 13 weeks)", ui.pct(sm["p_shortfall"], 0), "model_estimate",
              help="Share of simulated paths whose balance falls below the minimum in any week.")
    ui.metric(k[1], "Expected shortfall", ui.usd_short(sm["expected_shortfall_mean"]), "model_estimate")
    ui.metric(k[2], "Shortfall VaR 95%", ui.usd_short(sm["shortfall_var95"]), "model_estimate")
    ui.metric(k[3], "Shortfall CVaR 95%", ui.usd_short(sm["shortfall_cvar95"]), "model_estimate",
              help="Mean of the worst 5% of shortfalls.")
    ui.metric(k[4], "Cash-at-risk 95% (receipts)", ui.usd_short(sm["cash_at_risk_95"]), "model_estimate",
              help="Mean 13-week receipts minus their 5th percentile.")

    st.subheader("Weekly receipts: 13-week fan chart")
    fig = go.Figure()
    _band(fig, weeks, q["p02_5"], q["p97_5"], "95% interval", 0.15)
    _band(fig, weeks, q["p10"], q["p90"], "80% interval", 0.30)
    fig.add_scatter(x=weeks, y=q["p50"], name="Median", mode="lines", line=dict(width=2, color=ui.ACCENT),
                    hovertemplate="week %{x}: median $%{y:,.0f}<extra></extra>")
    fig.add_scatter(x=weeks, y=[need] * K, name="Weekly cash need", mode="lines",
                    line=dict(width=2, dash="dash", color=ui.NEUTRAL), hovertemplate="need $%{y:,.0f}<extra></extra>")
    if sim["actual"] is not None:
        fig.add_scatter(x=weeks, y=sim["actual"], name="Actual payer deposits", mode="markers",
                        marker=dict(size=9, color=ui.SERIES[1]), hovertemplate="week %{x}: actual $%{y:,.0f}<extra></extra>")
    fig.update_xaxes(dtick=1)
    ui.chart(ui.style(fig, y_title="Payer receipts per week", x_title=f"Week after {f}"), "model_estimate",
             f"cashflow.forecast at {f}, 2,000 paths, pipeline seed (actuals: synthetic_record bank deposits)")

    st.subheader("Cash position against the minimum balance")
    pos = sim["opening"] + np.cumsum(sim["sims"], axis=1) - need * weeks
    pq = np.percentile(pos, [5, 50, 95], axis=0)
    fig = go.Figure()
    fig.add_scatter(x=weeks, y=pq[2], mode="lines", line=dict(width=0), showlegend=False, hoverinfo="skip")
    fig.add_scatter(x=weeks, y=pq[0], mode="lines", line=dict(width=0), fill="tonexty", name="90% of paths",
                    fillcolor="rgba(42,120,214,0.22)", hoverinfo="skip")
    fig.add_scatter(x=weeks, y=pq[1], mode="lines", name="Median position", line=dict(width=2, color=ui.ACCENT),
                    hovertemplate="week %{x}: $%{y:,.0f}<extra></extra>")
    fig.add_scatter(x=weeks, y=[min_bal] * K, mode="lines", name="Minimum balance",
                    line=dict(width=2, dash="dot", color=ui.NEGATIVE))
    fig.update_xaxes(dtick=1)
    ui.chart(ui.style(fig, y_title="Opening balance + receipts - need", x_title="Week"), "model_estimate",
             f"opening bank balance {ui.usd(sim['opening'])} (synthetic_record) + simulated receipts - weekly need "
             "(financial_assumption)")
    pb = pd.DataFrame({"week": weeks, "P(balance below minimum)": sm["p_below_min_by_week"],
                       "expected from open claims": sim["expected"]["open_claims"],
                       "expected from new claims": sim["expected"]["new_claims"],
                       "in transit": sim["expected"]["in_transit"]})
    with st.expander("Week by week"):
        ui.table(pb, "model_estimate", "cashflow.forecast / cashflow.shortfall_metrics",
                 money_cols=["expected from open claims", "expected from new claims", "in transit"],
                 pct_cols=["P(balance below minimum)"])

    st.subheader("Backtest: did the intervals cover what happened?")
    bts = ((res.get("backtest") or {}).get("value") or {})
    summ = (bts.get("summary") or {}).get("hybrid") or {}
    if summ:
        b = st.columns(4)
        def cov(key):
            v = summ.get(key) or {}
            return v.get("coverage") if isinstance(v, dict) else v
        ui.metric(b[0], "Weekly 80% interval coverage", ui.pct(cov("weekly_80"), 1), "model_estimate")
        ui.metric(b[1], "Weekly 95% interval coverage", ui.pct(cov("weekly_95"), 1), "model_estimate")
        ui.metric(b[2], "13-week total, 80% coverage", ui.pct(cov("total_13w_80_all_origins"), 1), "model_estimate")
        ui.metric(b[3], "13-week total error (MAPE)", ui.pct(summ.get("total_13w_mape"), 1), "model_estimate")
    tot = pd.DataFrame([r for r in bts.get("totals_13w", []) if r.get("lag_model") == "hybrid"])
    if len(tot):
        tot["forecast_date"] = pd.to_datetime(tot["forecast_date"])
        fig = go.Figure()
        fig.add_scatter(x=tot["forecast_date"], y=tot["p90"], mode="lines", line=dict(width=0), showlegend=False, hoverinfo="skip")
        fig.add_scatter(x=tot["forecast_date"], y=tot["p10"], mode="lines", line=dict(width=0), fill="tonexty",
                        name="80% interval", fillcolor="rgba(42,120,214,0.25)", hoverinfo="skip")
        fig.add_scatter(x=tot["forecast_date"], y=tot["mean"], mode="lines", name="Forecast mean",
                        line=dict(width=2, color=ui.ACCENT), hovertemplate="%{x|%d %b %Y}: $%{y:,.0f}<extra></extra>")
        fig.add_scatter(x=tot["forecast_date"], y=tot["actual"], mode="markers", name="Actual",
                        marker=dict(size=8, color=ui.SERIES[1]), hovertemplate="%{x|%d %b %Y}: actual $%{y:,.0f}<extra></extra>")
        ui.chart(ui.style(fig, y_title="13-week payer receipts", x_title="Forecast date"), "model_estimate",
                 "results/cashflow.json backtest (each forecast uses only rows dated on or before its date)")
        st.caption(summ.get("note", ""))
    elif "backtest" in d:
        ui.table(d["backtest"].head(200), "model_estimate", "mart.cash_forecast_backtest")
