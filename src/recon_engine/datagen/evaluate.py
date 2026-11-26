"""Score the reconciliation against the generator's planted ground truth.

This is an EVALUATION script: it is the only package code (with tests) allowed to read
``data/truth``. It writes ``results/reconciliation.json`` with matching precision and recall per
rule and per defect type, what the rules miss and why, and the control-total results.

All numbers are counts over simulated records (label ``synthetic_record``). They measure how
well the rules recover structure planted by this project's own generator; they say nothing
about matching rates on real remittances.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from .. import RESULTS_DIR
from .generate import TRUTH_DIR

LABEL = "synthetic_record"


def _frame(conn, q: str, params: tuple = ()) -> pd.DataFrame:
    with conn.cursor() as cur:
        cur.execute(q, params)
        cols = [d.name for d in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=cols)


def _ratio(num: int, den: int) -> float | None:
    return round(num / den, 6) if den else None


CLAIM_CASE_NOTES = {
    "none": "claim number echoed as sent",
    "padding": "claim number left-padded with zeros",
    "prefix": "claim number with a PCN / CLM# prefix",
    "punctuation": "claim number with inserted hyphens",
    "lowercase": "claim number in lower case",
    "transposed": "two adjacent digits transposed; only member + date + amount is left",
    "transposed_id_with_twin_claim": "transposed claim number AND the patient has a second claim that day for the same service and charge",
    "repeated_service_with_line_control": "claim legitimately bills the same service twice; payer echoes line control numbers",
    "repeated_service_without_line_control": "claim legitimately bills the same service twice; payer echoes no line control number, so both remittance lines are verbatim identical",
}


def _claim_case(r) -> str:
    if r["id_defect"] == "transposed_twin":
        return "transposed_id_with_twin_claim"
    if bool(r["has_identical_sibling"]):
        return "repeated_service_with_line_control" if bool(r["line_control_echoed"]) else "repeated_service_without_line_control"
    return r["id_defect"]


def evaluate_claim_remit(conn, truth_dir: Path) -> dict[str, Any]:
    t = pd.read_csv(truth_dir / "remit_line_truth.csv", dtype={"trace_no": str, "true_claim_id": str})
    db = _frame(conn, """
        select r.src_row_no as remit_row_no, m.rule_id, c.claim_id, c.line_no, c.procedure_code as m_proc,
               c.line_charge as m_charge, (d.remit_line_id is not null) as flagged_duplicate,
               q.reason as exception_reason
        from core.fact_remit_line r
        left join recon.claim_remit_match m on m.remit_line_id = r.remit_line_id
        left join core.fact_claim_line c on c.claim_line_id = m.claim_line_id
        left join recon.remit_duplicate d on d.remit_line_id = r.remit_line_id
        left join recon.exception_queue q on q.exception_key = 'UNMATCHED_REMIT:' || r.remit_line_id and q.status = 'OPEN'
        where r.src_file = 'remittance_advice.csv'""")
    rej = _frame(conn, """select src_row_no as remit_row_no, reason_code from stg.reject
                          where source_table = 'raw.remittance_advice' and src_file = 'remittance_advice.csv'""")
    lines = _frame(conn, "select claim_id as true_claim_id, line_no as true_line_no, procedure_code as t_proc, line_charge as t_charge from core.fact_claim_line")
    df = t.merge(db, on="remit_row_no", how="left").merge(rej, on="remit_row_no", how="left")
    df = df.merge(lines.astype({"true_line_no": "float64"}), on=["true_claim_id", "true_line_no"], how="left")
    df["outcome"] = "UNMATCHED"
    df.loc[df["exception_reason"].fillna("").str.startswith("AMBIGUOUS"), "outcome"] = "QUEUED_AMBIGUOUS"
    df.loc[df["reason_code"].notna(), "outcome"] = "REJECTED_IN_STAGING"
    df.loc[df["flagged_duplicate"] == True, "outcome"] = "EXCLUDED_DUPLICATE"  # noqa: E712
    df.loc[df["rule_id"].notna(), "outcome"] = "MATCHED"
    df["exact"] = (df["outcome"] == "MATCHED") & (df["claim_id"] == df["true_claim_id"]) & (
        df["line_no"].astype("Int64") == df["true_line_no"].astype("Int64"))
    # matched to a different line of the right claim that is identical (same code and charge): the money
    # lands on an interchangeable line; counted correct but reported separately
    df["sibling"] = (df["outcome"] == "MATCHED") & ~df["exact"] & (df["claim_id"] == df["true_claim_id"]) & (
        df["m_proc"] == df["t_proc"]) & (df["m_charge"] == df["t_charge"])
    df["correct"] = df["exact"] | df["sibling"]
    df["case"] = df.apply(_claim_case, axis=1)

    per_rule = {}
    for rule, g in df[df["outcome"] == "MATCHED"].groupby("rule_id"):
        per_rule[rule] = {"matched": int(len(g)), "correct": int(g["correct"].sum()),
                          "of_which_identical_sibling_line": int(g["sibling"].sum()),
                          "wrong": int((~g["correct"]).sum()), "precision": _ratio(int(g["correct"].sum()), len(g))}
    post = df[df["expected_action"] == "POST"]
    by_case = {}
    for k, g in post.groupby("case"):
        m = g[g["outcome"] == "MATCHED"]
        q = g[g["outcome"].isin(["UNMATCHED", "QUEUED_AMBIGUOUS"])]
        dup = g[g["outcome"] == "EXCLUDED_DUPLICATE"]
        by_case[k] = {"description": CLAIM_CASE_NOTES.get(k, k), "lines": int(len(g)),
                      "matched_correct": int(g["correct"].sum()), "matched_wrong": int(len(m) - m["correct"].sum()),
                      "wrongly_excluded_as_duplicate": int(len(dup)),
                      "sent_to_exception_queue": int(len(q)), "of_which_ambiguous": int((q["outcome"] == "QUEUED_AMBIGUOUS").sum()),
                      "precision": _ratio(int(m["correct"].sum()), len(m)),
                      "recall": _ratio(int(g["correct"].sum()), len(g)),
                      "exception_queue_share": _ratio(len(q), len(g)),
                      "rules_used": {kk: int(v) for kk, v in m["rule_id"].value_counts().items()}}
    by_kind = {k: {"lines": int(len(g)), "correct": int(g["correct"].sum()), "recall": _ratio(int(g["correct"].sum()), len(g))}
               for k, g in post.groupby("kind")}
    matched = df[df["outcome"] == "MATCHED"]
    wrong = matched[~matched["correct"]]
    missed = post[~post["correct"]]
    dups = df[df["kind"] == "FILE_DUPLICATE"]
    flagged = df[df["outcome"] == "EXCLUDED_DUPLICATE"]
    orphans = df[df["expected_action"] == "UNMATCHABLE_ORPHAN"]
    rejected_line = df[df["expected_action"] == "UNMATCHABLE_REJECTED_CLAIM_LINE"]
    staging = df[df["expected_action"] == "REJECT_STAGING"]
    plb = df[df["expected_action"] == "PROVIDER_LEVEL"]
    return {
        "universe": {"remittance_rows": int(len(df)), "expected_to_post": int(len(post)),
                     "outcomes": {k: int(v) for k, v in df["outcome"].value_counts().items()}},
        "overall": {"matched": int(len(matched)), "correct": int(matched["correct"].sum()),
                    "of_which_identical_sibling_line": int(matched["sibling"].sum()),
                    "precision": _ratio(int(matched["correct"].sum()), len(matched)),
                    "recall_on_postable_lines": _ratio(int(post["correct"].sum()), len(post)),
                    "exception_queue_share_of_postable": _ratio(int(post["outcome"].isin(["UNMATCHED", "QUEUED_AMBIGUOUS"]).sum()), len(post))},
        "per_rule": per_rule,
        "by_case": by_case,
        "recall_by_line_kind": by_kind,
        "duplicate_detection": {
            "planted_file_duplicates": int(len(dups)), "flagged": int(len(flagged)),
            "true_positives": int((flagged["kind"] == "FILE_DUPLICATE").sum()),
            "false_positives": int((flagged["kind"] != "FILE_DUPLICATE").sum()),
            "false_positives_by_case": {k: int(v) for k, v in flagged[flagged["kind"] != "FILE_DUPLICATE"]["case"].value_counts().items()},
            "precision": _ratio(int((flagged["kind"] == "FILE_DUPLICATE").sum()), len(flagged)),
            "recall": _ratio(int((dups["outcome"] == "EXCLUDED_DUPLICATE").sum()), len(dups)),
            "missed_by_case": {k: int(v) for k, v in dups[dups["outcome"] != "EXCLUDED_DUPLICATE"]["case"].value_counts().items()},
        },
        "orphans_from_other_facility": {"lines": int(len(orphans)),
                                        "correctly_left_unmatched": int((orphans["outcome"] != "MATCHED").sum()),
                                        "wrongly_matched": int((orphans["outcome"] == "MATCHED").sum())},
        "provider_level_rows": {"lines": int(len(plb)), "wrongly_matched_to_claims": int((plb["outcome"] == "MATCHED").sum())},
        "remits_for_claim_lines_rejected_in_staging": {
            "lines": int(len(rejected_line)),
            "outcomes": {k: int(v) for k, v in rejected_line["outcome"].value_counts().items()},
            "note": "the claim line never reached core, so no rule can match it; it is queued as UNMATCHED_REMIT"},
        "remit_rows_with_bad_values": {"lines": int(len(staging)),
                                       "rejected_in_staging": int((staging["outcome"] == "REJECTED_IN_STAGING").sum())},
        "wrong_matches": {"count": int(len(wrong)),
                          "by_rule": {k: int(v) for k, v in wrong["rule_id"].value_counts().items()},
                          "by_case": {k: int(v) for k, v in wrong["case"].value_counts().items()},
                          "examples": wrong.head(5)[["remit_row_no", "kind", "case", "true_claim_id", "true_line_no",
                                                     "claim_id", "line_no", "rule_id"]].astype(str).to_dict("records")},
        "missed_postable_lines": {"count": int(len(missed)),
                                  "by_outcome": {k: int(v) for k, v in missed["outcome"].value_counts().items()},
                                  "by_case": {k: int(v) for k, v in missed["case"].value_counts().items()},
                                  "by_kind": {k: int(v) for k, v in missed["kind"].value_counts().items()}},
    }


BANK_CASE_NOTES = {
    "clean": "trace number intact in the bank description",
    "posting_date_shift": "bank posts one business day after the payer's effective date",
    "truncated_trace": "bank description cut off inside the trace number",
    "no_trace_in_bank_description": "no trace in the bank description; payer and amount only",
    "lockbox_bundle": "several paper checks deposited as one lockbox credit",
    "split_deposit_both_traced": "one payment arrives as two credits, both carrying the trace",
    "split_deposit_second_untraced": "one payment arrives as two credits, the second without a trace",
    "equal_amount_provider_payment_traced": "fixed twice-weekly provider-level payment with trace",
    "equal_amount_provider_payment_untraced": "fixed twice-weekly provider-level payment without trace: same payer, same amount, days apart",
    "deposit_missing": "payer says it paid; money never arrived (should stay unmatched)",
    "deposit_after_as_of": "deposit posts after the snapshot date (should stay unmatched, in transit)",
}


def evaluate_remit_bank(conn, truth_dir: Path, as_of: str) -> dict[str, Any]:
    t = pd.read_csv(truth_dir / "trace_truth.csv", dtype={"trace_no": str, "deposit_bank_row_nos": str})
    t["trace_key"] = t["payer_id"] + ":" + t["trace_no"]
    db = _frame(conn, """select m.trace_key, m.rule_id, b.src_row_no as bank_row_no
                         from recon.remit_bank_match m join core.fact_bank_txn b using (bank_txn_id)
                         where b.src_file = 'bank_statement.csv'""")
    core_traces = _frame(conn, "select trace_key from recon.remit_trace")
    queued = _frame(conn, """select trace_key, reason from recon.exception_queue
                             where status = 'OPEN' and exception_type in ('REMIT_NO_DEPOSIT', 'IN_TRANSIT')""")
    t = t[(t["remit_exported"]) & (t["total_cents"] > 0) & (t["payment_method"].isin(["ACH", "CHK"]))]
    t = t[t["trace_key"].isin(core_traces["trace_key"])].copy()
    got = db.groupby("trace_key").agg(rows=("bank_row_no", lambda x: frozenset(int(v) for v in x)),
                                      rule_id=("rule_id", lambda x: ",".join(sorted(set(x))))).reset_index()
    df = t.merge(got, on="trace_key", how="left").merge(queued, on="trace_key", how="left")
    df["truth_rows"] = df["deposit_bank_row_nos"].fillna("").map(lambda s: frozenset(int(x) for x in s.split(";") if x))
    df["deposit_in_statement"] = df["deposit_bank_row_no"].notna() & (df["deposit_posting_date"].fillna("9999") <= as_of)
    df["matched"] = df["rule_id"].notna()
    df["correct"] = df["matched"] & (df["rows"] == df["truth_rows"])
    df["queued"] = df["reason"].notna()
    df["ambiguous"] = df["reason"].fillna("").str.startswith("AMBIGUOUS")

    def cat(r) -> str:
        if r["deposit_missing"]:
            return "deposit_missing"
        if not r["deposit_in_statement"]:
            return "deposit_after_as_of"
        if r["provider_level"]:
            return "equal_amount_provider_payment_untraced" if r["no_trace_in_bank"] else "equal_amount_provider_payment_traced"
        if r["split_deposit"]:
            return "split_deposit_second_untraced" if r["split_second_part_no_trace"] else "split_deposit_both_traced"
        if r["payment_method"] == "CHK":
            return "lockbox_bundle"
        if r["no_trace_in_bank"]:
            return "no_trace_in_bank_description"
        if r["truncated_trace"]:
            return "truncated_trace"
        if r["posting_shift_bdays"] > 0:
            return "posting_date_shift"
        return "clean"

    df["category"] = df.apply(cat, axis=1)
    per_rule = {}
    for rule, g in df[df["matched"]].groupby("rule_id"):
        per_rule[rule] = {"payments_matched": int(len(g)), "correct": int(g["correct"].sum()),
                          "wrong": int((~g["correct"]).sum()), "precision": _ratio(int(g["correct"].sum()), len(g))}
    by_cat = {}
    for c, g in df.groupby("category"):
        expect_match = c not in ("deposit_missing", "deposit_after_as_of")
        base = {"description": BANK_CASE_NOTES.get(c, c), "payments": int(len(g))}
        if expect_match:
            m = g[g["matched"]]
            base.update({"matched_correct": int(g["correct"].sum()), "matched_wrong": int(len(m) - m["correct"].sum()),
                         "sent_to_exception_queue": int(g["queued"].sum()), "of_which_ambiguous": int(g["ambiguous"].sum()),
                         "precision": _ratio(int(m["correct"].sum()), len(m)),
                         "recall": _ratio(int(g["correct"].sum()), len(g)),
                         "exception_queue_share": _ratio(int(g["queued"].sum()), len(g)),
                         "rules_used": {k: int(v) for k, v in g["rule_id"].value_counts().items()}})
        else:
            base.update({"correctly_left_unmatched": int((~g["matched"]).sum()), "wrongly_matched": int(g["matched"].sum())})
        by_cat[c] = base
    should = df[df["deposit_in_statement"] & ~df["deposit_missing"]]
    matched = df[df["matched"]]
    b = pd.read_csv(truth_dir / "bank_truth.csv")
    no_remit = b[b["kind"] == "PAYER_EFT_NO_REMIT"]
    unapplied = _frame(conn, """select b.src_row_no as bank_row_no, q.reason from recon.exception_queue q join core.fact_bank_txn b using (bank_txn_id)
                                where q.status = 'OPEN' and q.exception_type = 'UNAPPLIED_CASH'""")
    return {
        "universe": {"payments_with_money_in_core": int(len(df)), "expected_to_match": int(len(should))},
        "overall": {"matched": int(len(matched)), "correct": int(matched["correct"].sum()),
                    "precision": _ratio(int(matched["correct"].sum()), len(matched)),
                    "recall": _ratio(int(should["correct"].sum()), len(should)),
                    "exception_queue_share": _ratio(int(should["queued"].sum()), len(should))},
        "per_rule": per_rule,
        "by_case": by_cat,
        "unapplied_cash": {"planted_deposits_without_remittance": int(len(no_remit)),
                           "queued_as_unapplied": int(len(unapplied)),
                           "planted_and_queued": int(no_remit["bank_row_no"].isin(unapplied["bank_row_no"]).sum()),
                           "queued_with_reason": {k: int(v) for k, v in unapplied["reason"].str.extract(r"^([A-Z ]+?):")[0].fillna("NO REMITTANCE").value_counts().items()}},
    }


LIMITS = [
    {"case": "transposed_id_with_twin_claim (no line control number)",
     "why": ("The identifier points nowhere and two claim lines share member, date, code and charge. Nothing in the "
             "remittance says which visit was paid, so the rule queues it as AMBIGUOUS for a person to check the "
             "payer's claim number (ICN) against the payer portal.")},
    {"case": "repeated_service_without_line_control + transposed or mangled claim id",
     "why": ("Two verbatim-identical remittance lines are only accepted as two services when the claim can be found "
             "and has two identical lines. If the claim number is unreadable, the second copy looks like a file "
             "duplicate and is excluded; the payer's own claim totals (CLP03/CLP04) would be needed to resolve it.")},
    {"case": "split_deposit_second_untraced",
     "why": ("The traced credit does not equal the payment and the untraced credit matches nothing on amount, so both "
             "credits go to UNAPPLIED_CASH (reason POSSIBLE SPLIT) and the payment to REMIT_NO_DEPOSIT. A person "
             "allocates them; the cash identity still ties because all three sit in explicit buckets.")},
    {"case": "equal_amount_provider_payment_untraced",
     "why": ("Two untraced credits of the same amount from the same payer a few days apart fit two payments equally "
             "well. Choosing the nearest date would usually be right but is a guess, so the rule queues both as AMBIGUOUS.")},
    {"case": "orphan reversals",
     "why": "A reversal whose original posting never arrived (its remittance file is missing) cannot be paired; it is queued."},
]


def evaluate_orphan_reversals(conn, truth_dir: Path) -> dict[str, Any]:
    w = pd.read_csv(truth_dir / "withheld_remit_lines.csv", dtype={"trace_no": str})
    orph = _frame(conn, """select c.claim_id, c.line_no from recon.exception_queue q
                           join core.fact_claim_line c on c.claim_line_id = q.claim_line_id
                           where q.status = 'OPEN' and q.exception_type = 'ORPHAN_REVERSAL'""")
    keys = set(zip(w["claim_id"], w["line_no"]))
    rt = pd.read_csv(truth_dir / "remit_line_truth.csv", dtype={"true_claim_id": str})
    rej = rt[rt["expected_action"] == "REJECT_STAGING"]
    rkeys = set(zip(rej["true_claim_id"], rej["true_line_no"].astype(int)))
    explained = sum((cid, ln) in keys for cid, ln in zip(orph["claim_id"], orph["line_no"]))
    explained_rej = sum(((cid, ln) in rkeys) and ((cid, ln) not in keys) for cid, ln in zip(orph["claim_id"], orph["line_no"]))
    return {"orphan_reversals": int(len(orph)), "original_was_on_a_withheld_remittance": int(explained),
            "original_row_rejected_in_staging": int(explained_rej),
            "unexplained": int(len(orph) - explained - explained_rej),
            "note": ("a reversal is 'orphan' when the posting it reverses never reached us; in this data that happens "
                     "when the earlier payment's remittance file was withheld (the money arrived as unapplied cash)")}


def evaluate_line_amounts(conn, truth_dir: Path) -> dict[str, Any]:
    t = pd.read_csv(truth_dir / "claim_line_truth.csv")
    db = _frame(conn, "select claim_id, line_no, paid from recon.claim_line_balance")
    db["paid_cents"] = (db["paid"].astype(float) * 100).round().astype("int64")
    df = t.merge(db, on=["claim_id", "line_no"], how="inner")
    eq_vis = (df["paid_cents"] == df["asof_paid_visible_cents"])
    eq_all = (df["paid_cents"] == df["asof_paid_cents"])
    return {"claim_lines_compared": int(len(df)),
            "posted_paid_equals_paid_on_received_remittances": int(eq_vis.sum()),
            "share_equal_to_received": _ratio(int(eq_vis.sum()), len(df)),
            "posted_paid_equals_all_payer_payments": int(eq_all.sum()),
            "share_equal_to_all_payments": _ratio(int(eq_all.sum()), len(df)),
            "note": ("the gap between the two shares is money the payer paid on remittances that never arrived "
                     "(unapplied cash): the hospital cannot post it to lines until the remittance is obtained")}


def evaluate_staging(conn, truth_dir: Path) -> dict[str, Any]:
    s = pd.read_csv(truth_dir / "staging_defects.csv")
    table_of = {"claims_export.csv": "raw.claims_export", "remittance_advice.csv": "raw.remittance_advice",
                "bank_statement.csv": "raw.bank_statement", "payer_contracts.csv": "raw.payer_contracts",
                "drug_dispense_log.csv": "raw.drug_dispense_log", "claim_status_history.csv": "raw.claim_status_history"}
    rej = _frame(conn, "select source_table, src_file, src_row_no, reason_code from stg.reject")
    out = {}
    for f, g in s.groupby("file"):
        r = rej[(rej["source_table"] == table_of[f]) & (rej["src_file"] == f)]
        planted = set(g["row_no"])
        got = set(r["src_row_no"])
        out[f] = {"planted_bad_rows": len(planted), "rejected_rows": len(got),
                  "planted_and_rejected": len(planted & got), "rejected_not_planted": len(got - planted),
                  "reasons": {k: int(v) for k, v in r["reason_code"].value_counts().items()}}
    return out


def run_evaluation(conn, truth_dir: Path = TRUTH_DIR, results_dir: Path = RESULTS_DIR) -> dict[str, Any]:
    as_of = conn.execute("select recon.param('as_of_date')").fetchone()[0]
    controls = _frame(conn, "select check_name, scope, expected, actual, difference, passed, detail from recon.control_totals order by check_id")
    days = conn.execute("select count(*) filter (where status = 'CLOSED'), count(*) from recon.daily_recon").fetchone()
    exc = _frame(conn, """select exception_type, severity, count(*) as n, coalesce(sum(amount), 0) as amount
                          from recon.exception_queue where status = 'OPEN' group by 1, 2 order by 1""")
    res = {
        "label": LABEL,
        "note": ("Evaluation of the reconciliation rules against ground truth planted by this project's generator. "
                 "Counts over simulated records; not evidence about real-world matching performance."),
        "as_of_date": as_of,
        "tie_out": {
            "all_control_totals_passed": bool(controls["passed"].all()),
            "checks": int(len(controls)),
            "max_abs_difference": float(controls["difference"].abs().max()),
            "days_closed": int(days[0]), "days_in_calendar": int(days[1]),
            "control_totals": [{k: (float(v) if k in ("expected", "actual", "difference") else v) for k, v in r.items()}
                               for r in controls.to_dict("records")],
        },
        "claim_to_remittance": evaluate_claim_remit(conn, truth_dir),
        "remittance_to_bank": evaluate_remit_bank(conn, truth_dir, as_of),
        "orphan_reversals": evaluate_orphan_reversals(conn, truth_dir),
        "what_the_rules_cannot_resolve": LIMITS,
        "line_paid_amounts": evaluate_line_amounts(conn, truth_dir),
        "staging_rejects_vs_planted": evaluate_staging(conn, truth_dir),
        "open_exceptions": [{"exception_type": r["exception_type"], "severity": r["severity"], "n": int(r["n"]),
                             "amount": float(r["amount"])} for r in exc.to_dict("records")],
    }
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / "reconciliation.json").write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")
    return res
