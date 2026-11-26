"""Reconciliation desk: revenue-cycle / cash-posting analyst."""
from __future__ import annotations

import io
import time
from datetime import date

import pandas as pd
import streamlit as st

from .. import batches, demo_db, loaders, state, ui

TITLE = "Reconciliation desk"
JOB = "Revenue-cycle / cash-posting analyst"
QUESTION = "Did today's payments post, and which items need a person?"


def _read_upload(f, expected: list[str], what: str) -> pd.DataFrame | None:
    if f is None:
        return None
    try:
        df = pd.read_csv(io.BytesIO(f.getvalue()), dtype=str, keep_default_na=False)
    except Exception as e:  # noqa: BLE001
        st.error(f"{what}: could not read the CSV ({e}).")
        return None
    cols = [c.strip().lower() for c in df.columns]
    unknown = [c for c in cols if c not in expected]
    if unknown:
        st.error(f"{what}: unexpected columns {unknown}. Expected the raw export format "
                 f"(see docs/data_dictionary.md): {', '.join(expected)}")
        return None
    df.columns = cols
    return df


def _run(remittance=None, bank=None, claim=None, label_hint: str = "desk") -> None:
    batch_label = f"{label_hint}{time.strftime('%Y%m%dT%H%M%S')}"
    from recon_engine import pipeline

    with st.spinner("Staging, matching, rebuilding balances and exceptions, closing the day ..."):
        with state.connection() as c:
            before = loaders.desk_snapshot(c)
            t0 = time.time()
            try:
                r = pipeline.ingest_and_reconcile(c, remittance=remittance, bank_statement=bank, claim=claim,
                                                  batch_label=batch_label)
            except Exception as e:  # noqa: BLE001
                c.rollback()
                st.session_state["desk_error"] = f"{type(e).__name__}: {e}"
                return
            secs = time.time() - t0
            after = loaders.desk_snapshot(c)
    st.session_state.pop("desk_error", None)
    st.session_state["desk_last"] = {"result": r, "before": before, "after": after, "seconds": secs,
                                     "label": batch_label}
    state.clear()


def _reset() -> None:
    with st.spinner("Restoring the baseline database ..."):
        try:
            demo_db.recreate_demo()
        except Exception as e:  # noqa: BLE001
            st.session_state["desk_error"] = str(e)
            return
    for k in ("desk_last", "desk_batch", "desk_error"):
        st.session_state.pop(k, None)
    state.clear()
    st.session_state["desk_reset_done"] = True


def _status_bar(snap: dict) -> None:
    s = state.startup()["status"]
    c1, c2, c3, c4 = st.columns([1, 1, 1, 1.2])
    c1.metric("As-of date", snap["as_of"])
    c2.metric("Days closed by recon.close_day", f"{snap['days_closed']} of {snap['days_total']}")
    c3.metric("Control totals", "all tie" if snap["all_passed"] else "BREAK",
              help="24 checks in recon.control_totals, to the cent")
    with c4:
        st.write("")
        st.button("Reset demo database", on_click=_reset, disabled=s.hosted, type="secondary",
                  help="Drops the demo copy and makes it again from the pipeline database (under a second).")
        st.caption(f"Writing to `{s.database}`; baseline `{s.baseline}` is never modified.")
    if st.session_state.pop("desk_reset_done", False):
        st.success("Baseline restored: every intake was removed and the numbers are back to the pipeline build.")
    if s.message and not st.session_state.get("desk_seen_status"):
        st.info(s.message)
        st.session_state["desk_seen_status"] = True


def _inputs() -> None:
    tab_up, tab_sample, tab_claim = st.tabs(["Upload a batch", "Sample batches", "Enter one claim"])

    with tab_up:
        st.markdown("Upload a remittance file (835-like) and/or a bank statement in the raw export formats. "
                    "Either one alone works: a remittance without its deposit waits as *in transit*; "
                    "a deposit without a remittance becomes *unapplied cash*.")
        a, b = st.columns(2)
        f_r = a.file_uploader("Remittance advice CSV", type=["csv"], key="up_remit")
        f_b = b.file_uploader("Bank statement CSV", type=["csv"], key="up_bank")
        r = _read_upload(f_r, batches.REMIT_COLUMNS, "Remittance")
        bk = _read_upload(f_b, batches.BANK_COLUMNS, "Bank statement")
        if r is not None:
            a.caption(f"{len(r)} remittance rows, {r['trace_no'].nunique()} payment(s)")
        if bk is not None:
            b.caption(f"{len(bk)} bank rows")
        if st.button("Run reconciliation on the uploaded files", type="primary",
                     disabled=(r is None and bk is None)):
            _run(remittance=r, bank=bk, label_hint="upload")
            st.rerun()

    with tab_sample:
        st.markdown("**Fresh batch.** A new payer payment built from claims in the database that have no remittance "
                    "yet, plus the bank rows on which it arrives. It contains the defects the rules exist for. "
                    "Download the two files and upload them, or run them here.")
        n = st.slider("Claims to pay", 2, 8, 4, key="fresh_n")
        if st.button("Generate a fresh batch"):
            with state.connection() as c:
                try:
                    st.session_state["desk_batch"] = batches.fresh_batch(c, n_claims=n, seed=int(time.time()))
                except RuntimeError as e:
                    st.warning(str(e))
        bt = st.session_state.get("desk_batch")
        if bt is not None:
            st.markdown("What is inside (simulated):")
            st.markdown("\n".join(f"- {x}" for x in bt.notes))
            d1, d2 = st.columns(2)
            d1.download_button("Download remittance_batch.csv", batches.to_csv_bytes(bt.remittance),
                               "remittance_batch.csv", "text/csv")
            d2.download_button("Download bank_statement_batch.csv", batches.to_csv_bytes(bt.bank),
                               "bank_statement_batch.csv", "text/csv")
            with st.expander("Preview the raw rows"):
                st.dataframe(bt.remittance, hide_index=True)
                st.dataframe(bt.bank, hide_index=True)
            r1, r2, r3 = st.columns(3)
            if r1.button("Run remittance + bank", type="primary"):
                _run(remittance=bt.remittance, bank=bt.bank, label_hint="fresh")
                st.session_state.pop("desk_batch", None)
                st.rerun()
            if r2.button("Run remittance only"):
                _run(remittance=bt.remittance, label_hint="fresh")
                st.session_state["desk_batch"] = batches.Batch(bt.remittance.iloc[0:0], bt.bank, bt.notes, bt.trace_no,
                                                               bt.payer_id, bt.payment_total)
                st.rerun()
            if r3.button("Run bank only"):
                _run(bank=bt.bank, label_hint="fresh")
                st.session_state.pop("desk_batch", None)
                st.rerun()
        st.divider()
        st.markdown("**Replay of rows already received.** The first rows of the original exports in "
                    "`data/samples/`. Loading them again should post nothing: remittance lines are excluded as "
                    "duplicates and bank rows are rejected for a repeated bank reference.")
        rr, rb = batches.replay_samples(12)
        e1, e2, e3 = st.columns(3)
        e1.download_button("Download remittance sample", batches.to_csv_bytes(rr), "remittance_replay.csv", "text/csv")
        e2.download_button("Download bank sample", batches.to_csv_bytes(rb), "bank_replay.csv", "text/csv")
        if e3.button("Run the replay"):
            _run(remittance=rr, bank=rb, label_hint="replay")
            st.rerun()

    with tab_claim:
        _claim_form()


def _claim_form() -> None:
    with state.connection() as c:
        payers = loaders.frame(c, "select payer_id, payer_name from core.dim_payer order by payer_id")
        procs = batches.procedure_options(c)
        next_id = batches.next_claim_id(c)
        asof = batches.as_of_date(c)
    st.markdown("Enter one claim as the billing system would export it. It lands as a new claim line with no "
                "remittance (status SUBMITTED): billed charges and *not yet adjudicated* rise on the CFO screen. "
                "A fresh sample batch then pays hand-entered claims first.")
    with st.form("claim_form"):
        a, b, c3 = st.columns(3)
        claim_id = a.text_input("Claim number", next_id)
        payer = b.selectbox("Payer", payers["payer_id"], format_func=lambda p: f"{p} {payers.set_index('payer_id').loc[p, 'payer_name']}")
        member = c3.text_input("Member id", "DEMO123456789")
        d1, d2, d3 = st.columns(3)
        service_date = d1.date_input("Service date", asof, max_value=asof)
        proc = d2.selectbox("Procedure", procs["procedure_code"],
                            format_func=lambda p: f"{p} ({procs.set_index('procedure_code').loc[p, 'service_line_code']})")
        units = d3.number_input("Units", min_value=1, max_value=500, value=1)
        typical = float(procs.set_index("procedure_code").loc[proc, "typical_charge_per_unit"] or 100.0)
        charge = st.number_input("Line charge (USD)", min_value=0.01, value=round(typical * units, 2), step=10.0,
                                 help="Defaults to the typical simulated charge per unit for the procedure chosen when the form opened.")
        go = st.form_submit_button("Submit claim and reconcile", type="primary")
    if go:
        p = procs.set_index("procedure_code").loc[proc]
        rows = batches.manual_claim_rows(
            claim_id=claim_id.strip(), payer_id=payer, payer_name=payers.set_index("payer_id").loc[payer, "payer_name"],
            member_id=member.strip(), service_line=p["service_line_code"], service_date=service_date,
            submission_date=asof, lines=[{"procedure_code": proc, "revenue_code": p["revenue_code"], "units": int(units),
                                          "line_charge": charge}])
        _run(claim=rows, label_hint="claim")
        st.rerun()


def _result() -> None:
    last = st.session_state.get("desk_last")
    if st.session_state.get("desk_error"):
        st.error("The intake failed and was rolled back: " + st.session_state["desk_error"])
    if not last:
        st.info("No batch run in this session yet. Use one of the three inputs above.")
        return
    r, before, after = last["result"], last["before"], last["after"]
    st.subheader("Result of the last run")
    st.caption(f"Batch `{last['label']}` processed in {last['seconds']:.1f} s. Label of every number in this section: "
               "synthetic_record (simulated records and sums over them).")
    landed = sum(r["landed"].values())
    cm, bm, ex, rj = r["claim_matches"], r["bank_matches"], r["new_exceptions"], r["rejects"]
    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("Raw rows landed", f"{landed:,}")
    k2.metric("Rejected in staging", f"{len(rj):,}")
    matched = cm[cm["rule_id"].notna()] if len(cm) else cm
    k3.metric("Remittance lines posted to claims", f"{len(matched):,} of {len(cm):,}")
    k4.metric("New open exceptions", f"{len(ex):,}")
    cl = r.get("close") or {}
    refused = cl.get("refused")
    k5.metric("Day close", "refused" if refused else (f"{cl.get('closed', 0)} day(s) closed" if cl else "not run"))
    if refused:
        st.error(f"recon.close_day refused {refused['date']}: {refused['message']}")

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Matches by rule**")
        rows = []
        if len(cm):
            g = cm.assign(rule=cm["rule_id"].fillna("not matched (see exceptions)")).groupby("rule")["paid_amount"]
            rows += [{"stage": "claim to remittance", "rule": k, "lines": int(v.count()), "paid": float(v.astype(float).sum())}
                     for k, v in g]
        if len(bm):
            g = bm.assign(rule=bm["rule_id"].fillna("not matched (see exceptions)")).groupby("rule")["amount"]
            rows += [{"stage": "remittance to bank", "rule": k, "lines": int(v.count()), "paid": float(v.astype(float).sum())}
                     for k, v in g]
        if rows:
            st.dataframe(pd.DataFrame(rows), hide_index=True,
                         column_config={"paid": ui.money_col("amount")})
        else:
            st.write("No remittance or bank rows in this batch.")
    with c2:
        st.markdown("**New exceptions, with the reason a person reads**")
        if len(ex):
            st.dataframe(ex[["exception_type", "severity", "claim_id", "trace_key", "amount", "reason"]], hide_index=True,
                         column_config={"amount": ui.money_col("amount")})
        else:
            st.write("None.")
    if len(rj):
        with st.expander(f"Staging rejects ({len(rj)})"):
            st.dataframe(rj, hide_index=True)
    with st.expander("Claim-side detail (every remittance line in the batch)"):
        st.dataframe(cm, hide_index=True)
    with st.expander("Bank-side detail"):
        st.dataframe(bm, hide_index=True)

    st.subheader("Control identity before and after")
    st.latex(r"\text{billed} = \text{paid} + \text{contractual} + \text{denied} + \text{patient resp.} + \text{open balance}")
    comp = loaders.compare_snapshots(before, after)
    st.dataframe(comp, hide_index=True,
                 column_config={c: ui.money_col(c) for c in ("before", "after", "change")})
    ib, ia = before["identity"], after["identity"]
    chk = lambda i: i["billed"] - (i["paid"] + i["contractual"] + i["denied"] + i["patient_resp"] + i["open_balance"])  # noqa: E731
    ct_b = before["controls"].set_index("check_name")
    ct_a = after["controls"].set_index("check_name")
    rows = [{"check": "claim identity residual (sum over all lines)", "before": chk(ib), "after": chk(ia)}]
    for k in loaders.KEY_CHECKS:
        if k in ct_a.index:
            rows.append({"check": f"{k}: {ct_a.loc[k, 'detail'] or ct_a.loc[k, 'scope']}",
                         "before": float(ct_b.loc[k, "difference"]) if k in ct_b.index else None,
                         "after": float(ct_a.loc[k, "difference"])})
    st.dataframe(pd.DataFrame(rows), hide_index=True,
                 column_config={"before": ui.money_col("difference before"),
                                "after": ui.money_col("difference after")})
    ok = after["all_passed"]
    (st.success if ok else st.error)(
        f"All {len(after['controls'])} control totals tie to the cent after the intake." if ok else
        "A control total breaks after the intake; see recon.control_totals.")
    st.info("Open **Q1 Hospital CFO** in the sidebar: billed, collected, the stuck-cash buckets and A/R aging now "
            "include this batch.")


def render() -> None:
    ui.header(JOB, QUESTION,
              "Three-way reconciliation, live: claim to remittance to bank deposit. Land a batch in the raw export "
              "format and the same SQL that built the database (`stg.process_new`, `core.process_new`, "
              "`recon.run_all`, `mart.refresh_q1`, `recon.close_day`) runs on it.")
    ui.simulated_note()
    if not state.require_db():
        return
    snap = state.load("desk")
    _status_bar(snap)
    with state.connection() as c:
        hist = loaders.intake_history(c)
    if len(hist):
        with st.expander(f"Batches landed since the last reset ({hist['batch'].nunique()})"):
            st.dataframe(hist, hide_index=True)
    st.subheader("1. Bring in a batch")
    _inputs()
    st.subheader("2. What happened")
    _result()
    with st.expander("The matching rules (recon.match_rule: rules are data, not code)"):
        st.dataframe(snap["rules"], hide_index=True)
    with st.expander("All open exceptions by type (current database)"):
        st.dataframe(snap["exceptions"], hide_index=True,
                     column_config={"amount": ui.money_col("amount")})
