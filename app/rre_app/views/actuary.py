"""Q4 Payer actuary: how much is still owed on claims incurred but not yet paid?"""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from .. import loaders, state, ui

TITLE = "Q4 Payer actuary"
JOB = "Payer actuary"
QUESTION = "How much is still owed on claims incurred but not yet paid?"


def render() -> None:
    ui.header(JOB, QUESTION, "A paid development triangle built in SQL from reconciled remittances (service month down "
              "the side, months since service across the top), projected with chain-ladder, Mack standard errors, an "
              "over-dispersed Poisson bootstrap and a Bornhuetter-Ferguson check (`models/reserve.py`).")
    ui.simulated_note()
    if not state.require_db():
        return
    d = state.load("actuary")
    if d["missing"] and "triangle" not in d:
        ui.not_built(d["missing"])
        return
    r = d["results"] or {}
    cl = r.get("chain_ladder") or {}
    v = loaders.val
    boot = cl.get("bootstrap") or {}
    k = st.columns(4)
    ui.metric(k[0], "Unpaid, chain-ladder", ui.usd_short(v(cl.get("total_unpaid"))), "model_estimate")
    ui.metric(k[1], "90% bootstrap interval",
              f"{ui.usd_short(v(boot.get('p05')))} to {ui.usd_short(v(boot.get('p95')))}", "model_estimate")
    ui.metric(k[2], "Mack standard error", ui.usd_short(v(cl.get("mack_se_total"))), "model_estimate",
              help=f"Coefficient of variation {ui.pct(v(cl.get('mack_cv')))}")
    ui.metric(k[3], "Unpaid, Bornhuetter-Ferguson", ui.usd_short(v(cl.get("bf_cape_cod_unpaid"))), "model_estimate",
              help="Cape Cod expected loss ratio on contract allowed of reported claims.")
    if d["missing"]:
        ui.not_built(d["missing"], "Part of this screen")

    if "triangle" in d:
        st.subheader("Development triangle (cumulative paid)")
        tri = d["triangle"].copy()
        tri["accident_month"] = pd.to_datetime(tri["accident_month"]).dt.strftime("%Y-%m")
        piv = tri.pivot(index="accident_month", columns="dev_lag", values="cumulative_paid")
        lags = [c for c in piv.columns if c <= 18]
        fig = go.Figure(go.Heatmap(z=piv[lags].values, x=[str(c) for c in lags], y=piv.index, colorscale="Blues",
                                   hovertemplate="service month %{y}, lag %{x}: $%{z:,.0f}<extra></extra>",
                                   colorbar=dict(title="Cumulative paid", tickprefix="$")))
        fig.update_yaxes(autorange="reversed")
        ui.chart(ui.style(fig, height=520, money=False, legend=False, x_title="Months since service"),
                 "synthetic_record", "mart.dev_triangle (paid net of reversals, provider-level rows excluded; ties to "
                 "recon.claim_line_balance.paid)")
        with st.expander("Triangle as a table"):
            tbl = piv[lags].rename(columns=lambda c: f"lag {c}").reset_index()
            ui.table(tbl, "synthetic_record", "mart.dev_triangle", money_cols=[f"lag {c}" for c in lags])

    if "factors" in d:
        st.subheader("Chain-ladder factors")
        fct = d["factors"]
        c1, c2 = st.columns([1.2, 1])
        with c1:
            fig = go.Figure(go.Bar(x=[f"{a}-{b}" for a, b in zip(fct["dev_lag_from"], fct["dev_lag_to"])],
                                   y=fct["age_to_age"], marker_color=ui.ACCENT,
                                   hovertemplate="lag %{x}: f = %{y:.3f}<extra></extra>"))
            fig.update_yaxes(type="log")
            ui.chart(ui.style(fig, height=320, money=False, legend=False, y_title="Age-to-age factor (log)",
                              x_title="Development lag"), "model_estimate", "mart.reserve_factors")
        with c2:
            ui.table(fct[["dev_lag_from", "dev_lag_to", "n_periods", "age_to_age", "cumulative_to_ultimate"]].head(12),
                     "model_estimate", "mart.reserve_factors")
        st.caption("The lag-0 factor multiplies the newest month by about 8.6, so one noisy month moves the estimate; "
                   "that is why Bornhuetter-Ferguson is reported beside chain-ladder.")

    if "by_period" in d:
        st.subheader("Unpaid by service month, with interval")
        bp = d["by_period"].copy()
        bp["accident_month"] = pd.to_datetime(bp["accident_month"])
        bp = bp[bp["unpaid_cl"].abs() > 0.5]
        fig = go.Figure()
        fig.add_bar(x=bp["accident_month"], y=bp["unpaid_cl"], name="Chain-ladder", marker_color=ui.SERIES[0],
                    error_y=dict(type="data", symmetric=False, array=bp["boot_p95"] - bp["unpaid_cl"],
                                 arrayminus=(bp["unpaid_cl"] - bp["boot_p05"]).clip(lower=0), thickness=1.2, width=3),
                    hovertemplate="%{x|%b %Y}: $%{y:,.0f}<extra></extra>")
        fig.add_scatter(x=bp["accident_month"], y=bp["unpaid_bf"], name="Bornhuetter-Ferguson", mode="markers",
                        marker=dict(size=9, color=ui.SERIES[1], symbol="diamond"),
                        hovertemplate="BF %{x|%b %Y}: $%{y:,.0f}<extra></extra>")
        ui.chart(ui.style(fig, y_title="Unpaid", x_title="Service month"), "model_estimate",
                 "mart.reserve_by_period (whiskers: ODP bootstrap 5th to 95th percentile)")

    if "retro" in d:
        st.subheader("Retrospective test: estimate then versus what was paid afterwards")
        rt = d["retro"].copy()
        rt["retro_as_of"] = pd.to_datetime(rt["retro_as_of"]).dt.date.astype(str)
        fig = go.Figure()
        fig.add_bar(x=rt["retro_as_of"], y=rt["unpaid_cl"], name="Chain-ladder estimate", marker_color=ui.SERIES[0],
                    error_y=dict(type="data", symmetric=False, array=rt["boot_p95"] - rt["unpaid_cl"],
                                 arrayminus=rt["unpaid_cl"] - rt["boot_p05"], thickness=1.2, width=4))
        fig.add_bar(x=rt["retro_as_of"], y=rt["unpaid_bf"], name="Bornhuetter-Ferguson", marker_color=ui.SERIES[2])
        fig.add_scatter(x=rt["retro_as_of"], y=rt["paid_later_observed"], name="Paid afterwards (observed)", mode="markers",
                        marker=dict(size=11, color=ui.SERIES[1], symbol="line-ew-open", line=dict(width=3)))
        fig.update_layout(barmode="group")
        ui.chart(ui.style(fig, y_title="Unpaid at the earlier date", x_title="Earlier as-of date"), "model_estimate",
                 "mart.reserve_retro (each estimate uses only data known on its date)")
        rt["cl_over_observed"] = rt["unpaid_cl"] / rt["paid_later_observed"]
        rt["bf_over_observed"] = rt["unpaid_bf"] / rt["paid_later_observed"]
        rt["inside_cl_90"] = (rt["paid_later_observed"] >= rt["boot_p05"]) & (rt["paid_later_observed"] <= rt["boot_p95"])
        ui.table(rt[["retro_as_of", "unpaid_cl", "unpaid_bf", "paid_later_observed", "cl_over_observed", "bf_over_observed",
                     "inside_cl_90"]], "model_estimate", "mart.reserve_retro",
                 money_cols=["unpaid_cl", "unpaid_bf", "paid_later_observed"])
        st.markdown("The four dates share data, so they are not four independent checks. Both methods tend to "
                    "underestimate; Bornhuetter-Ferguson's error range is narrower.")
