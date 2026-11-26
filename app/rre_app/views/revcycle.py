"""Q2 Revenue-cycle lead: which denied claims should staff work first today?"""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from .. import loaders, state, ui

TITLE = "Q2 Revenue-cycle lead"
JOB = "Revenue-cycle lead"
QUESTION = "Which denied claims should staff work first today?"

POLICY_TEXT = {"MODEL_KNAPSACK": "Model + knapsack", "OLDEST_FIRST": "Oldest first", "LARGEST_FIRST": "Largest first"}


def render() -> None:
    ui.header(JOB, QUESTION, "Each denied claim is worth P(overturn) x what the payer would actually pay minus the staff "
              "cost of the appeal, and nothing once the appeal window has closed. Today's list is the set of claims "
              "with the highest total value that fits in the staff hours, chosen by an exact 0/1 knapsack "
              "(`models/prioritize.py`).")
    ui.simulated_note()
    if not state.require_db():
        return
    d = state.load("revcycle")
    if "queue" not in d:
        ui.not_built(d["missing"])
        return
    q = d["queue"]
    res = d["results"] or {}
    a = d["assumptions"]
    st.markdown(f"**Staff hours** {ui.badge('financial_assumption')}")
    hours = st.slider("Staff hours available today for denial appeals", 1.0, 40.0, float(d["staff_hours_default"]), 0.5,
                      help=f"Default from config/calibration.yaml: {a['denial_staff_hours_per_day']['value']} hours. "
                           f"Handling cost {ui.usd(a['appeal_handling_cost_usd']['value'], 0)} per "
                           f"{a['appeal_handling_minutes']['value']} minutes (financial_assumption), scaled by an "
                           "illustrative effort multiplier per denial category.")
    sol = loaders.solve_worklist(q, hours)
    pol = sol["policies"]
    c = st.columns(4)
    ui.metric(c[0], "Claims to work today", f"{pol['MODEL_KNAPSACK']['n_worked']} of {len(q)}", "model_estimate",
              help="Claims whose latest status is DENIED with no action yet.")
    ui.metric(c[1], "Expected net recovery", ui.usd(pol["MODEL_KNAPSACK"]["expected_net"]), "model_estimate")
    ui.metric(c[2], "vs oldest first", ui.usd(pol["MODEL_KNAPSACK"]["expected_net"] - pol["OLDEST_FIRST"]["expected_net"]),
              "model_estimate", help="Extra expected dollars from the same hours.")
    ui.metric(c[3], "vs largest first", ui.usd(pol["MODEL_KNAPSACK"]["expected_net"] - pol["LARGEST_FIRST"]["expected_net"]),
              "model_estimate")

    comp = pd.DataFrame([{"policy": POLICY_TEXT[k], "claims": v["n_worked"], "minutes used": v["minutes"],
                          "expected net": v["expected_net"]} for k, v in pol.items()])
    fig = go.Figure(go.Bar(x=comp["policy"], y=comp["expected net"], marker_color=[ui.SERIES[0], ui.NEUTRAL, ui.NEUTRAL],
                           text=[ui.usd_short(v) for v in comp["expected net"]], textposition="outside",
                           hovertemplate="%{x}: $%{y:,.0f}<extra></extra>"))
    left, right = st.columns([1, 1.3])
    with left:
        ui.chart(ui.style(fig, height=320, legend=False, y_title=f"Expected net, {hours:g} h"), "model_estimate",
                 "prioritize.choose on mart.work_queue (same budget for all three rules)")
    with right:
        st.markdown(f"With **{hours:g} hours** ({sol['capacity_minutes']:,} minutes) the knapsack picks "
                    f"**{pol['MODEL_KNAPSACK']['n_worked']}** claims worth **{ui.usd(pol['MODEL_KNAPSACK']['expected_net'])}** "
                    f"in expected net recovery. Working the oldest denials first would earn "
                    f"{ui.usd(pol['OLDEST_FIRST']['expected_net'])}; the largest bills first, "
                    f"{ui.usd(pol['LARGEST_FIRST']['expected_net'])}.")
        st.markdown("Value is dominated by the dollar amount, so *largest first* comes close; the model adds the rest by "
                    "skipping claims whose overturn chance is too low to cover the handling cost. When hours exceed the "
                    "backlog, all three rules pick the same claims.")
        ui.table(comp, "model_estimate", "prioritize.choose", money_cols=["expected net"])

    st.subheader("Today's work list")
    lst = sol["list"]
    show = lst[["work_today", "claim_id", "payer_id", "reason_category", "denial_date", "days_left_to_appeal", "billed",
                "p_overturn", "expected_recoverable", "handling_minutes", "expected_value", "reason"]].copy()
    show["denial_date"] = show["denial_date"].dt.date
    ui.table(show, "model_estimate", "mart.work_queue re-solved for the hours above (reason text from the model run "
             "at the configured hours)", money_cols=["billed", "expected_recoverable", "expected_value"],
             pct_cols=["p_overturn"], height=420)
    st.caption("The reason column was written by the model at the configured staff hours; the 'work_today' column "
               "is the re-solved choice for the hours you set.")

    if "backtest" in d:
        st.subheader("Did the rule work in the past?")
        bt = d["backtest"]
        g = bt[bt["pool"] == "APPEALED_WITH_OUTCOME"]
        fig = go.Figure()
        for i, p in enumerate(("MODEL_KNAPSACK", "LARGEST_FIRST", "OLDEST_FIRST")):
            s = g[g["policy"] == p].sort_values("hours_per_day")
            fig.add_scatter(x=s["hours_per_day"], y=s["realised_net"], name=POLICY_TEXT[p], mode="lines+markers",
                            line=dict(width=2, color=ui.SERIES[i]), marker=dict(size=8),
                            hovertemplate=POLICY_TEXT[p] + "<br>%{x} h/day: $%{y:,.0f}<extra></extra>")
        fig.update_xaxes(type="log")
        ui.chart(ui.style(fig, y_title="Realised net dollars, held-out period", x_title="Staff hours per day (log scale)"),
                 "model_estimate", "mart.work_queue_backtest, pool APPEALED_WITH_OUTCOME: daily replay after the "
                 "2025-07-01 cutoff, valued with what the payers actually paid")
        st.markdown("Honest reading: on held-out appeals the logistic model is better than one overall rate but only "
                    "about as good as a table of overturn rates by denial reason (see docs/model_notes_ops.md). The "
                    "ranking matters when hours are scarce; above about 6 hours a day every rule works the whole inflow.")
        hm = ((res.get("validation") or {}).get("held_out_metrics") or {})
        if hm:
            rows = [{"predictor": k.replace("_", " "), "n": v.get("n"), "Brier": v.get("brier"), "log loss": v.get("log_loss"),
                     "AUC": v.get("auc"), "calibration slope": v.get("calibration_slope")} for k, v in hm.items()]
            ui.table(pd.DataFrame(rows), "model_estimate",
                     "results/prioritize.json, appeals decided after the 2025-07-01 cutoff (lower Brier is better)")
