"""CFO operations report: a short PDF with a storyline for the hospital CFO.

    ~/.venvs/receivables-engine/bin/python scripts/build_reports.py        # writes reports/cfo_report.pdf

Every number is read from the pipeline: the Q1 marts and reconciliation tables in the built
database (``recon_engine.db``, honouring RRE_PG_DIR / DATABASE_URL) and the model outputs in
``results/*.json``. Nothing is typed in by hand. The data are simulated for a fictional hospital,
and the report says so on every page.

Layout follows the owner's PDF style guide: serif LaTeX-like type (Computer Modern from
matplotlib's bundled fonts, so the build is offline and portable), centred title, grey running
header with a thin rule, navy section headings with thin rules, booktabs-style tables, grey page
number centred in the footer.
"""
from __future__ import annotations

import io
import json
import os
from datetime import date
from pathlib import Path
from typing import Any

from .. import REPO_ROOT, RESULTS_DIR, db

OUT_PATH = REPO_ROOT / "reports" / "cfo_report.pdf"

NAVY = "#24406e"
BLUE = "#4a74ad"
GREY = "#7a7a7a"
RULE = "#9a9a9a"

LABEL_WORDS = {"synthetic_record": "synthetic_record", "model_estimate": "model_estimate",
               "financial_assumption": "financial_assumption", "observed_public": "observed_public",
               "published_aggregate": "published_aggregate"}

# Plain-language reason and owner for each stuck-cash bucket (domain text, not numbers).
BUCKET_WHY = {
    "DENIED_IN_PLAY": ("Denied, claim not written off; appeal or correct", "Denials team"),
    "PROVIDER_LEVEL_SUSPENSE": ("Payer-level adjustments not tied to any claim", "Payer relations"),
    "UNAPPLIED_CASH": ("Money in the bank with no remittance to post it", "Cash posting"),
    "REMIT_NO_DEPOSIT": ("Payer says it paid; no deposit found", "Cash posting, treasury"),
    "ORPHAN_REVERSAL_LINES": ("Reversal of a payment whose file never came", "Payer relations"),
    "NOT_YET_ADJUDICATED": ("Submitted, no remittance yet", "Follow-up team"),
    "UNDERPAID_VS_CONTRACT": ("Allowed below the contract rate", "Contract management"),
    "PARTIAL_PAYMENT_BALANCE": ("Balance left after a partial payment", "Follow-up team"),
    "UNMATCHED_REMITTANCE": ("Remittance line with no claim found", "Cash posting"),
    "CREDIT_BALANCE": ("Overpaid; owed back to payers (liability)", "Compliance"),
}
BUCKET_NAME = {
    "DENIED_IN_PLAY": "Denied, still appealable", "PROVIDER_LEVEL_SUSPENSE": "Provider-level suspense",
    "UNAPPLIED_CASH": "Unapplied cash", "REMIT_NO_DEPOSIT": "Remitted, never deposited",
    "ORPHAN_REVERSAL_LINES": "Orphan reversals", "NOT_YET_ADJUDICATED": "Not yet adjudicated",
    "UNDERPAID_VS_CONTRACT": "Underpaid vs contract", "PARTIAL_PAYMENT_BALANCE": "Partial-payment balance",
    "UNMATCHED_REMITTANCE": "Unmatched remittance", "CREDIT_BALANCE": "Credit balances",
    "DENIED_WRITTEN_OFF": "Denied and written off", "IN_TRANSIT": "In transit",
}


# ------------------------------------------------------------------------------------------
# facts: every number the report quotes
# ------------------------------------------------------------------------------------------
def _val(x: Any) -> Any:
    return x.get("value") if isinstance(x, dict) and "value" in x and "label" in x else x


def _results(name: str, results_dir: Path) -> dict:
    p = results_dir / f"{name}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def gather(conn, results_dir: Path = RESULTS_DIR) -> dict[str, Any]:
    """Collect the report's numbers from the database and results/. Missing model outputs leave
    their keys at None so the text can say the analysis has not been run."""
    q1 = lambda s, p=(): conn.execute(s, p).fetchone()  # noqa: E731
    f: dict[str, Any] = {}
    f["as_of"] = date.fromisoformat(q1("select recon.param('as_of_date')")[0])
    t = q1("""select sum(billed), sum(contractual_adjustment), sum(denied), sum(patient_responsibility), sum(payer_paid),
                     sum(open_balance), sum(contract_expected), min(service_month), max(service_month), sum(n_lines)
              from mart.pnl_by_payer_service""")
    keys = ("billed", "contractual", "denied", "patient_resp", "paid", "open", "contract_expected", "first_month",
            "last_month", "n_lines")
    f["pnl"] = {k: (float(v) if k not in ("first_month", "last_month", "n_lines") else v) for k, v in zip(keys, t)}
    f["identity_residual"] = float(q1("""select sum(billed - paid - contractual - denied - patient_resp - open_balance)
                                         from recon.claim_line_balance""")[0])
    ct = q1("select count(*), bool_and(passed), max(abs(difference)) from recon.control_totals")
    f["controls"] = {"n": ct[0], "all_passed": bool(ct[1]), "max_diff": float(ct[2])}
    dd = q1("select count(*) filter (where status = 'CLOSED'), count(*) from recon.daily_recon")
    f["days_closed"], f["days_total"] = dd
    cp = q1("select sum(payer_cash_deposited), sum(payer_cash_matched), sum(unapplied_cash) from mart.cash_position")
    f["cash"] = {"deposited": float(cp[0]), "matched": float(cp[1]), "unapplied": float(cp[2])}
    f["stuck"] = [dict(zip(("bucket", "n", "amount"), (r[0], int(r[1]), float(r[2])))) for r in
                  conn.execute("select bucket, n_items, amount from mart.cash_stuck order by sort").fetchall()]
    sb = {r["bucket"]: r for r in f["stuck"]}
    f["stuck_by"] = sb
    amb = {(r[0], r[1]): (int(r[2]), float(r[3])) for r in conn.execute("""
        select exception_type, case when reason like 'AMBIGUOUS%%' then 'AMBIGUOUS'
                                    when reason like 'POSSIBLE SPLIT%%' then 'SPLIT' else 'OTHER' end, count(*), sum(amount)
        from recon.exception_queue where status = 'OPEN' and exception_type in ('UNAPPLIED_CASH', 'REMIT_NO_DEPOSIT')
        group by 1, 2""").fetchall()}
    f["pairs"] = amb
    ag = q1("""select sum(open_expected), sum(open_expected) filter (where aging_bucket >= '121-180') from mart.ar_aging""")
    f["aging"] = {"open_expected": float(ag[0] or 0), "older_120": float(ag[1] or 0)}
    f["payers"] = dict(conn.execute("select payer_id, payer_name from core.dim_payer").fetchall())

    pr, rs, dr, ec, cf, cr, rec = (_results(n, results_dir) for n in
                                   ("prioritize", "reserve", "drug", "econ", "cashflow", "credit", "reconciliation"))
    tq = pr.get("todays_queue") or {}
    pol = tq.get("policies_same_budget") or {}
    f["worklist"] = None if not tq else {
        "candidates": _val(tq.get("candidates")), "selected": _val(tq.get("selected")),
        "minutes": _val(tq.get("budget_minutes")), "expected": _val(tq.get("selected_expected_net")),
        "oldest": (pol.get("OLDEST_FIRST") or {}).get("expected_net"),
        "largest": (pol.get("LARGEST_FIRST") or {}).get("expected_net"),
        "realised_ratio": ((pr.get("validation") or {}).get("recoverable_amount_check") or {}).get("realised_over_predicted"),
        "auc_model": (((pr.get("validation") or {}).get("held_out_metrics") or {}).get("logistic_model") or {}).get("auc"),
        "brier_model": (((pr.get("validation") or {}).get("held_out_metrics") or {}).get("logistic_model") or {}).get("brier"),
        "brier_reason": (((pr.get("validation") or {}).get("held_out_metrics") or {}).get("baseline_reason_rate") or {}).get("brier"),
    }
    cl = rs.get("chain_ladder") or {}
    f["reserve"] = None if not cl else {
        "unpaid": _val(cl.get("total_unpaid")), "p05": _val((cl.get("bootstrap") or {}).get("p05")),
        "p95": _val((cl.get("bootstrap") or {}).get("p95")), "bf": _val(cl.get("bf_cape_cod_unpaid"))}
    dm = ((dr.get("totals") or {}).get("MATURE")) or {}
    los = ((dr.get("losers_mature") or {}).get("ranked") or [{}])
    f["drug"] = None if not dm else {
        "n_losing": _val(dm.get("n_drugs_losing_money")), "n_drugs": dm.get("n_drugs"),
        "margin": _val(dm.get("margin_collected")), "price_gap": _val(dm.get("price_gap_contract_minus_cost")),
        "denied": _val(dm.get("denied_leak")), "worst": los[0] if los else {}}
    hl = ec.get("headline") or {}
    under = {r.get("payer_id"): r for r in (_val(ec.get("underpayment_by_payer")) or {}).get("rows", [])} \
        if isinstance(_val(ec.get("underpayment_by_payer")), dict) else {}
    inc = _val(ec.get("incentives_by_payer"))
    thr = []
    if isinstance(inc, dict) and "rows" in inc:
        thr = [r.get("threshold_usd") for r in inc["rows"] if r.get("threshold_usd") is not None]
    elif isinstance(inc, list):
        thr = [r.get("threshold_usd") for r in inc if isinstance(r, dict) and r.get("threshold_usd") is not None]
    f["econ"] = None if not hl else {
        "shortfall": _val(hl.get("total_pricing_shortfall_usd")), "worst_payer": _val(hl.get("payer_highest_shortfall_rate")),
        "worst_rate": _val(hl.get("highest_shortfall_rate")), "worst_ci": _val(hl.get("highest_shortfall_rate_ci")),
        "above": _val(hl.get("total_shortfall_above_threshold_usd")), "net_recovery": _val(hl.get("total_expected_net_recovery_usd")),
        "float_late": _val(hl.get("total_float_late_usd")), "thr_min": min(thr) if thr else None, "thr_max": max(thr) if thr else None,
        "under": under}
    sf = _val(cf.get("shortfall")) or {}          # going-concern date (last backtest date)
    ro = _val(cf.get("shortfall_as_of_run_off")) or {}
    bts = ((_val(cf.get("backtest")) or {}).get("summary") or {}).get("hybrid") or {}
    cov = lambda k: (bts.get(k) or {}).get("coverage") if isinstance(bts.get(k), dict) else bts.get(k)  # noqa: E731
    f["cash_fc"] = None if not ro else {
        "ro_mean13": ro.get("receipts_13w_mean"), "ro_p05": ro.get("receipts_13w_p05"), "ro_p_short": ro.get("p_shortfall"),
        "ro_need": ro.get("weekly_cash_need"), "ro_cvar": ro.get("shortfall_cvar95"),
        "ss_date": sf.get("forecast_date"), "ss_mean13": sf.get("receipts_13w_mean"), "ss_actual13": sf.get("actual_13w_receipts"),
        "ss_p_short": sf.get("p_shortfall"), "ss_need": sf.get("weekly_cash_need"),
        "factor": _val(((cf.get("assumptions") or {}).get("cash_need_factor")) or {}),
        "cov80": cov("weekly_80"), "cov95": cov("weekly_95"), "mape13": bts.get("total_13w_mape")}
    ss = (_val(cr.get("certificate_steady_state")) or {})
    ao = (_val(cr.get("certificate_as_of")) or {})
    f["credit"] = None if not ss else {
        "date_ss": ss.get("certificate_date"), "avail_ss": (ss.get("totals") or {}).get("availability"),
        "eligible_ss": (ss.get("totals") or {}).get("eligible"), "el_ss": (ss.get("totals") or {}).get("expected_loss"),
        "avail_asof": (ao.get("totals") or {}).get("availability"), "rules": ss.get("rules") or {}}
    ov = (rec.get("claim_to_remittance") or {}).get("overall") or {}
    f["matching"] = {"precision": ov.get("precision"), "recall": ov.get("recall_on_postable_lines")} if ov else None
    return f


# ------------------------------------------------------------------------------------------
# formatting
# ------------------------------------------------------------------------------------------
def money(x: float | None, digits: int = 0) -> str:
    if x is None:
        return "n/a"
    s = "-" if x < 0 else ""
    return f"{s}${abs(x):,.{digits}f}"


def mm(x: float | None) -> str:
    """$2.29 million / $324,591."""
    if x is None:
        return "n/a"
    a = abs(x)
    s = "-" if x < 0 else ""
    if a >= 1e6:
        return f"{s}${a / 1e6:,.2f} million"
    return f"{s}${a:,.0f}"


def pct(x: float | None, d: int = 1) -> str:
    return "n/a" if x is None else f"{100 * x:.{d}f}%"


# ------------------------------------------------------------------------------------------
# PDF
# ------------------------------------------------------------------------------------------
def _fonts() -> None:
    import matplotlib
    from reportlab.lib.fonts import addMapping
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    d = Path(matplotlib.get_data_path()) / "fonts" / "ttf"
    if "CMR" in pdfmetrics.getRegisteredFontNames():
        return
    pdfmetrics.registerFont(TTFont("CMR", str(d / "cmr10.ttf")))
    pdfmetrics.registerFont(TTFont("CMB", str(d / "cmb10.ttf")))
    pdfmetrics.registerFont(TTFont("CMI", str(d / "cmti10.ttf")))
    pdfmetrics.registerFont(TTFont("STIX", str(d / "STIXGeneral.ttf")))
    addMapping("CMR", 0, 0, "CMR")
    addMapping("CMR", 1, 0, "CMB")
    addMapping("CMR", 0, 1, "CMI")
    addMapping("CMR", 1, 1, "CMB")


def _styles():
    from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
    from reportlab.lib.styles import ParagraphStyle

    base = dict(fontName="CMR", fontSize=10.5, leading=14.2)
    return {
        "title": ParagraphStyle("title", fontName="CMB", fontSize=19, leading=24, alignment=TA_CENTER, spaceAfter=4),
        "subtitle": ParagraphStyle("subtitle", fontName="CMR", fontSize=11.5, leading=15, alignment=TA_CENTER),
        "note": ParagraphStyle("note", fontName="CMR", fontSize=9.5, leading=12.5, alignment=TA_CENTER,
                               textColor=GREY, spaceBefore=3),
        "h1": ParagraphStyle("h1", fontName="CMB", fontSize=14, leading=18, textColor=NAVY, spaceBefore=12, spaceAfter=1),
        "h2": ParagraphStyle("h2", fontName="CMB", fontSize=11.2, leading=15, textColor=BLUE, spaceBefore=7, spaceAfter=2),
        "body": ParagraphStyle("body", alignment=TA_JUSTIFY, firstLineIndent=16, spaceAfter=3, **base),
        "body0": ParagraphStyle("body0", alignment=TA_JUSTIFY, firstLineIndent=0, spaceAfter=3, **base),
        "item": ParagraphStyle("item", alignment=TA_JUSTIFY, leftIndent=16, firstLineIndent=-12, spaceAfter=3.5, **base),
        "cell": ParagraphStyle("cell", fontName="CMR", fontSize=9.2, leading=11.4, alignment=TA_LEFT),
        "cellr": ParagraphStyle("cellr", fontName="CMR", fontSize=9.2, leading=11.4, alignment=TA_RIGHT),
        "cellh": ParagraphStyle("cellh", fontName="CMB", fontSize=9.2, leading=11.4, alignment=TA_LEFT),
        "cellhr": ParagraphStyle("cellhr", fontName="CMB", fontSize=9.2, leading=11.4, alignment=TA_RIGHT),
        "caption": ParagraphStyle("caption", fontName="CMR", fontSize=9, leading=11.5, alignment=TA_CENTER,
                                  textColor=GREY, spaceBefore=3, spaceAfter=6),
    }


def lab(label: str) -> str:
    """Small grey data-label tag (STIX so the underscore renders)."""
    return f'<font name="STIX" size="7.6" color="{GREY}">[{LABEL_WORDS[label]}]</font>'


def _u(txt: str) -> str:
    """Set identifiers with underscores (table names, labels) in STIX: Computer Modern's OT1 encoding has no
    underscore glyph. Bracketed label tags are already STIX."""
    import re
    return re.sub(r"(?<![\[\w])([A-Za-z][\w.]*_[\w.*]*)", r'<font name="STIX">\1</font>', txt)


def _esc(s: str) -> str:
    return s.replace("&", "&amp;")


def _table(rows: list[list], col_widths: list[float], styles: dict, right_cols: tuple = (), bold_last: bool = False):
    """Booktabs table: heavy top and bottom rules, light rule under the header, no vertical lines."""
    from reportlab.lib import colors
    from reportlab.platypus import Paragraph, Table, TableStyle

    data = []
    for i, r in enumerate(rows):
        out = []
        for j, c in enumerate(r):
            if i == 0:
                st = styles["cellhr"] if j in right_cols else styles["cellh"]
            else:
                st = styles["cellr"] if j in right_cols else styles["cell"]
            if bold_last and i == len(rows) - 1:
                c = f"<b>{c}</b>"
            out.append(Paragraph(_u(str(c)), st))
        data.append(out)
    t = Table(data, colWidths=col_widths, hAlign="CENTER", repeatRows=1)
    ts = [("LINEABOVE", (0, 0), (-1, 0), 1.0, colors.black), ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.black),
          ("LINEBELOW", (0, -1), (-1, -1), 1.0, colors.black), ("VALIGN", (0, 0), (-1, -1), "TOP"),
          ("TOPPADDING", (0, 0), (-1, -1), 2.2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.2),
          ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
          ("TOPPADDING", (0, 1), (-1, 1), 4)]
    if bold_last:
        ts.append(("LINEABOVE", (0, -1), (-1, -1), 0.5, colors.black))
    t.setStyle(TableStyle(ts))
    return t


def _stuck_figure(f: dict, width_in: float) -> io.BytesIO:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    order = ["DENIED_IN_PLAY", "PROVIDER_LEVEL_SUSPENSE", "UNAPPLIED_CASH", "REMIT_NO_DEPOSIT", "ORPHAN_REVERSAL_LINES",
             "NOT_YET_ADJUDICATED", "UNDERPAID_VS_CONTRACT", "PARTIAL_PAYMENT_BALANCE", "UNMATCHED_REMITTANCE", "CREDIT_BALANCE"]
    sb = f["stuck_by"]
    rows = [(BUCKET_NAME[b], sb[b]["amount"]) for b in order if b in sb]
    rows.sort(key=lambda r: r[1])
    with plt.rc_context({"font.family": "STIXGeneral", "mathtext.fontset": "stix", "font.size": 9,
                         "axes.spines.top": False, "axes.spines.right": False}):
        fig, ax = plt.subplots(figsize=(width_in, 2.55), dpi=220)
        names = [r[0] for r in rows]
        vals = [r[1] / 1e6 for r in rows]
        ax.barh(names, vals, color=[("#b54a3c" if v < 0 else NAVY) for v in vals], height=0.62)
        for y, v in enumerate(vals):
            ax.text(v + (0.03 if v >= 0 else -0.03), y, f"{'-' if v < 0 else ''}\\${abs(v):,.2f}M", va="center", ha="left" if v >= 0 else "right",
                    fontsize=8, color="#333333")
        ax.axvline(0, color="#555555", lw=0.6)
        ax.set_xlabel("Amount, millions of dollars (synthetic_record)", fontsize=8.5)
        ax.tick_params(axis="both", labelsize=8.5, length=0)
        ax.grid(axis="x", color="#e3e3e3", lw=0.6)
        ax.set_axisbelow(True)
        ax.set_xlim(min(0, min(vals)) - 0.35, max(vals) * 1.18)
        fig.tight_layout(pad=0.3)
        buf = io.BytesIO()
        fig.savefig(buf, format="png")
        plt.close(fig)
    buf.seek(0)
    return buf


def _story(f: dict, styles: dict, frame_w: float) -> list:
    from reportlab.lib.units import inch
    from reportlab.platypus import CondPageBreak, Image, KeepTogether, Paragraph, Spacer
    from reportlab.platypus.flowables import HRFlowable

    S = styles
    P = lambda txt, st="body": Paragraph(_u(txt), S[st])  # noqa: E731
    SR, ME, FA = lab("synthetic_record"), lab("model_estimate"), lab("financial_assumption")

    def h1(txt):
        return [CondPageBreak(1.3 * inch), Paragraph(txt, S["h1"]),
                HRFlowable(width="100%", thickness=0.6, color=RULE, spaceBefore=1, spaceAfter=6)]

    p, sb = f["pnl"], f["stuck_by"]
    g = lambda b: sb.get(b, {"amount": 0.0, "n": 0})  # noqa: E731
    actionable = ["DENIED_IN_PLAY", "PROVIDER_LEVEL_SUSPENSE", "UNAPPLIED_CASH", "REMIT_NO_DEPOSIT", "ORPHAN_REVERSAL_LINES",
                  "NOT_YET_ADJUDICATED", "UNDERPAID_VS_CONTRACT", "PARTIAL_PAYMENT_BALANCE", "UNMATCHED_REMITTANCE"]
    act_total = sum(g(b)["amount"] for b in actionable if b != "REMIT_NO_DEPOSIT")
    pairs = f["pairs"]
    amb = pairs.get(("UNAPPLIED_CASH", "AMBIGUOUS"), (0, 0.0))
    split_u = pairs.get(("UNAPPLIED_CASH", "SPLIT"), (0, 0.0))
    rnd_other = pairs.get(("REMIT_NO_DEPOSIT", "OTHER"), (0, 0.0))
    first, last = p["first_month"], p["last_month"]
    story: list = []

    story += [P("Where the Cash Is Stuck, and What to Do This Month", "title"),
              P(f"CFO operations report, Meridian Health System, as of {f['as_of']:%d %B %Y}", "subtitle"),
              P("Meridian Health System and its six payers are fictional. Every record behind this report is simulated, "
                "so the figures show how the reconciliation and the models work; they are not findings about real "
                "hospitals or payers. Each number carries its data label in brackets.", "note"),
              Spacer(1, 10)]

    # 1. situation -------------------------------------------------------------------------
    story += h1("1. Situation")
    story.append(P(
        f"<b>Bottom line.</b> The books tie to the cent, so the question is no longer whether cash is missing but where it "
        f"waits. Of {mm(p['billed'])} billed for services from {first:%B %Y} to {last:%B %Y}, the contracts entitle the "
        f"hospital to {mm(p['contract_expected'])}. Payers have paid {mm(p['paid'])}, which is "
        f"{pct(p['paid'] / p['contract_expected'])} of that entitlement, and patients owe {mm(p['patient_resp'])} {SR}. "
        f"A further <b>{mm(act_total)}</b> sits in places where a person can act this month (not counting remittances whose "
        f"money never arrived, which partly mirror the unapplied cash), led by "
        f"{BUCKET_NAME['DENIED_IN_PLAY'].lower()} claims ({mm(g('DENIED_IN_PLAY')['amount'])}), payer adjustments held in "
        f"suspense ({mm(g('PROVIDER_LEVEL_SUSPENSE')['amount'])}) and cash that reached the bank without a remittance "
        f"({mm(g('UNAPPLIED_CASH')['amount'])}) {SR}.", "body0"))
    c = f["controls"]
    story.append(P(
        f"The reconciliation is the reason these numbers can be trusted. Every claim line satisfies billed = paid + "
        f"contractual adjustment + denied + patient responsibility + open balance; summed over {p['n_lines']:,} lines the "
        f"residual is {money(f['identity_residual'], 2)}. All {c['n']} control totals pass, the largest difference is "
        f"{money(c['max_diff'], 2)}, and the daily close procedure closed {f['days_closed']:,} of {f['days_total']:,} days. "
        f"It refuses to close a day when the bank balance does not chain or a deposit is not fully allocated. Of "
        f"{mm(f['cash']['deposited'])} of payer money deposited, {mm(f['cash']['matched'])} is matched to remittances and "
        f"{mm(f['cash']['unapplied'])} is not {SR}."))
    story.append(P(
        f"One feature of the data changes how everything below should be read. The simulated service period ends in "
        f"{last:%B %Y}, so at the as-of date the receivables book is in run-off: no new claims arrive, and "
        f"{mm(f['aging']['older_120'])} of the {mm(f['aging']['open_expected'])} still open (expected value) is more than "
        f"120 days old {SR}. A hospital that is still billing would look younger and healthier."))
    tbl = [["Where the billed dollars went", "Amount", "Share of billed"],
           ["Contractual adjustment (agreed discount)", money(p["contractual"]), pct(p["contractual"] / p["billed"])],
           ["Paid by payers", money(p["paid"]), pct(p["paid"] / p["billed"])],
           ["Patient responsibility", money(p["patient_resp"]), pct(p["patient_resp"] / p["billed"])],
           ["Denied", money(p["denied"]), pct(p["denied"] / p["billed"])],
           ["Still open", money(p["open"]), pct(p["open"] / p["billed"])],
           ["Billed charges", money(p["billed"]), "100.0%"]]
    story.append(KeepTogether([Spacer(1, 4), _table(tbl, [3.0 * inch, 1.35 * inch, 1.1 * inch], S, right_cols=(1, 2), bold_last=True),
                               P("Table 1. Collected versus billed, all services. Data label: synthetic_record. "
                                 "Source: mart.pnl_by_payer_service.", "caption")]))

    # 2. where cash is stuck -----------------------------------------------------------------
    story += h1("2. Where the cash is stuck, and why")
    story.append(P(
        "The reconciliation sorts every dollar that is not cleanly paid into a named bucket. Table 2 lists the buckets "
        "that are not yet lost, largest first, with the reason the money is waiting and who can move it. Denials on "
        f"claims already written off ({mm(g('DENIED_WRITTEN_OFF')['amount'])} at contract value) are excluded: that money "
        f"is gone {SR}.", "body0"))
    rows = [["Bucket", "Items", "Amount", "Why it is waiting", "Owner"]]
    for b in sorted([b for b in actionable + ["CREDIT_BALANCE"] if b in sb], key=lambda b: -abs(sb[b]["amount"])):
        why, owner = BUCKET_WHY[b]
        rows.append([BUCKET_NAME[b], f"{sb[b]['n']:,}", money(sb[b]["amount"]), why, owner])
    story.append(KeepTogether([_table(rows, [1.45 * inch, 0.5 * inch, 0.95 * inch, 2.35 * inch, 1.15 * inch], S,
                                      right_cols=(1, 2)),
                               P(f"Table 2. Stuck cash as of {f['as_of']:%d %B %Y}. Data label: synthetic_record. Source: "
                                 "mart.cash_stuck; denied and unadjudicated lines valued at contract allowed.", "caption")]))
    story.append(KeepTogether([Image(_stuck_figure(f, 6.3), width=6.3 * inch, height=2.55 * inch),
                               P("Figure 1. The same buckets on one scale. Data label: synthetic_record.", "caption")]))
    story.append(Paragraph("Denials are the largest amount a team can recover", S["h2"]))
    d = f["drug"]
    txt = (f"{mm(g('DENIED_IN_PLAY')['amount'])} of contract value is denied on {g('DENIED_IN_PLAY')['n']:,} lines whose "
           f"claims are still open to appeal or correction {SR}. ")
    if d:
        w = d["worst"] or {}
        txt += (f"Denials fall hardest on expensive claims. In the drug analysis, {d['n_losing']} of {d['n_drugs']} drugs lose "
                f"money after reimbursement, yet contract rates exceed what the drugs cost by {mm(d['price_gap'])} {ME}; "
                f"the loss comes from {mm(d['denied'])} of drug contract value that was denied {SR}. The largest single "
                f"case, {_esc(w.get('description', ''))}, had {pct(w.get('denied_line_share'), 0)} of its claim lines denied.")
    story.append(P(txt, "body0"))
    story.append(Paragraph("Cash in the bank that cannot be posted", S["h2"]))
    story.append(P(
        f"{mm(g('UNAPPLIED_CASH')['amount'])} of payer money is in the bank without a remittance, while "
        f"{mm(g('REMIT_NO_DEPOSIT')['amount'])} of remittances report payments that never reached the bank {SR}. Much of "
        f"this is the same money seen from two sides. {mm(amb[1])} sits in pairs of equal amounts from the same payer a few "
        f"days apart, where the rules refuse to guess which credit belongs to which payment, and {mm(split_u[1])} of credits "
        f"look like one payment split across two deposits {SR}. A person can settle these from the payer portal in "
        "minutes per item; the rules deliberately leave them to a person rather than post a guess.", "body0"))
    story.append(Paragraph("Money parked outside the claims", S["h2"]))
    un_txt = ""
    e = f["econ"]
    if e:
        ci = e["worst_ci"] or [None, None]
        un_txt = (f" Underpayment against contract is smaller but systematic: {mm(g('UNDERPAID_VS_CONTRACT')['amount'])} on "
                  f"{g('UNDERPAID_VS_CONTRACT')['n']:,} lines {SR}. {_esc(f['payers'].get(e['worst_payer'], e['worst_payer']))} "
                  f"({e['worst_payer']}) has the highest shortfall rate, {pct(e['worst_rate'], 2)} of contract (95% interval "
                  f"{pct(ci[0], 2)} to {pct(ci[1], 2)}) {ME}.")
    story.append(P(
        f"{mm(g('PROVIDER_LEVEL_SUSPENSE')['amount'])} of provider-level adjustments are payer offsets that name no claim; "
        f"until each is agreed with the payer, the revenue they represent cannot be booked {SR}. A further "
        f"{mm(g('ORPHAN_REVERSAL_LINES')['amount'])} of billed charges on {g('ORPHAN_REVERSAL_LINES')['n']} lines carry a "
        f"reversal of a payment whose original remittance file never arrived, so the balance cannot be known until the "
        f"payer resends the file {SR}." + un_txt, "body0"))

    # 3. what to do this month ---------------------------------------------------------------
    story += h1("3. What to do this month")
    story.append(P("In priority order: cash already in the bank first, then the work that recovers the most per staff hour, "
                   "then the clean-up items. Each range comes from the reconciliation or a model; model estimates are "
                   "marked as such.", "body0"))
    wl = f["worklist"]
    acts = []
    acts.append((
        "Pair unapplied cash with the payments that never arrived.",
        f"{mm(amb[1])} to {mm(amb[1] + split_u[1])} postable once a person confirms the candidate pairs; at most "
        f"{mm(g('UNAPPLIED_CASH')['amount'])}",
        f"The money is already in the bank; posting it clears both buckets at once and corrects A/R {SR}."))
    if wl and wl["expected"] is not None:
        lo = wl["expected"] * (wl["realised_ratio"] or 1.0)
        acts.append((
            "Work the denial list by expected value, not by age.",
            f"{mm(lo)} to {mm(wl['expected'])} expected net from {wl['selected']} of {wl['candidates']} open denials in "
            f"{wl['minutes'] / 60:g} staff hours {ME}",
            f"Working the oldest denials first would earn {mm(wl['oldest'])} with the same hours, and the largest bills first "
            f"{mm(wl['largest'])} {ME}. The low end applies the {wl['realised_ratio']:.2f} ratio of realised to predicted "
            f"recovery seen on held-out appeals; staff hours are a {FA}."))
    acts.append((
        "Trace remittances that were never deposited.",
        f"{mm(rnd_other[1])} with no candidate deposit, up to {mm(g('REMIT_NO_DEPOSIT')['amount'])} in all",
        f"Ask each payer for EFT trace confirmation; anything not resolved by action 1 is either misdirected or unpaid {SR}."))
    if e and e["net_recovery"] is not None:
        acts.append((
            "Dispute underpayments above the break-even threshold only.",
            f"{mm(e['net_recovery'])} expected net after appeal costs, out of {mm(e['above'])} above threshold {ME}",
            f"Appeal when shortfall x P(win) exceeds the cost of an appeal: thresholds run from {money(e['thr_min'], 0)} to "
            f"{money(e['thr_max'], 0)} per claim by payer {ME}; the appeal cost is a {FA}."))
    acts.append((
        "Request the missing remittance files behind orphan reversals.",
        f"up to {mm(g('ORPHAN_REVERSAL_LINES')['amount'])} of billed charges to resolve; collectible share unknown",
        f"These lines cannot be closed or valued until the payer resends the original file {SR}."))
    acts.append((
        "Refund credit balances owed to payers.",
        f"{mm(abs(g('CREDIT_BALANCE')['amount']))} owed back on {g('CREDIT_BALANCE')['n']} lines",
        f"A liability, not revenue; refunding promptly avoids recoupment from future payments {SR}."))
    rows = [["", "Action", "Dollar range this month"]] + [[str(i + 1), a[0], a[1]] for i, a in enumerate(acts)]
    story.append(KeepTogether([_table(rows, [0.25 * inch, 2.75 * inch, 3.35 * inch], S),
                               P("Table 3. Actions in priority order. Labels are given in the text below.", "caption")]))
    for i, a in enumerate(acts):
        story.append(P(f"<b>{i + 1}. {a[0]}</b> {a[2]}", "item"))

    # 4. risks and limits --------------------------------------------------------------------
    story += h1("4. Risks and limits")
    cfc, crd, rsv = f["cash_fc"], f["credit"], f["reserve"]
    if cfc and cfc["ro_mean13"] is not None:
        story.append(P(
            f"<b>Cash runway.</b> With no new billing, the open book is expected to bring in {mm(cfc['ro_mean13'])} over the "
            f"next 13 weeks, and {mm(cfc['ro_p05'])} in a bad case (5th percentile) {ME}. If weekly spending stays at "
            f"{cfc['factor']:.2f} times recent weekly receipts ({money(cfc['ro_need'], 0)}) {FA}, "
            f"{pct(cfc['ro_p_short'], 0)} of simulated paths fall below the minimum balance, with a 95% CVaR shortfall of "
            f"{mm(cfc['ro_cvar'])} {ME}: collections alone cannot fund operations in run-off. The same model asked on "
            f"{cfc['ss_date']}, while the hospital was still billing, expected {mm(cfc['ss_mean13'])} over 13 weeks against "
            f"{mm(cfc['ss_actual13'])} that actually arrived, with no shortfall at a {money(cfc['ss_need'], 0)} weekly need "
            f"{ME}; across the backtest its 80% weekly intervals covered {pct(cfc['cov80'], 0)} of actual weeks and the 95% "
            f"intervals {pct(cfc['cov95'], 0)}. The method is calibrated; the shortfall is a property of the run-off book.",
            "body0"))
    if crd and crd["avail_ss"] is not None:
        r = crd["rules"]
        story.append(P(
            f"<b>Borrowing against receivables.</b> A receivables facility at an {pct(r.get('advance_rate'), 0)} advance rate "
            f"and a {r.get('max_age_days')}-day age limit {FA} would have lent {mm(crd['avail_ss'])} on the "
            f"{crd['date_ss']} certificate but "
            + (f"nothing today (availability {money(crd['avail_asof'], 0)} once credit balances are reserved) {ME}"
               if (crd['avail_asof'] or 0) <= 0 else f"{mm(crd['avail_asof'])} today {ME}")
            + ", because almost every open line is now past the age limit. Bridge funding has to come from elsewhere."))
    if rsv and rsv["unpaid"] is not None:
        story.append(P(
            f"<b>What is still to come.</b> Chain-ladder on the paid development triangle estimates {mm(rsv['unpaid'])} still "
            f"to be paid on services already delivered, with a 90% interval of {mm(rsv['p05'])} to {mm(rsv['p95'])} "
            f"{ME}; Bornhuetter-Ferguson gives {mm(rsv['bf'])}. A few large denied-and-appealed claims dominate this "
            "amount, so the outcome is lumpy."))
    lim = ("<b>Limits.</b> All records are simulated; the report demonstrates the method, not facts about any real payer. "
           "Matching accuracy was scored against the generator's own planted cases")
    if f["matching"]:
        lim += f" (claim-to-remittance precision {f['matching']['precision']}, recall {f['matching']['recall']})"
    lim += (", which checks internal consistency, not real-world performance. ")
    if wl and wl.get("brier_model") is not None:
        lim += (f"The denial-overturn model is only as good as a simple table of overturn rates by denial reason on held-out "
                f"appeals (Brier {wl['brier_model']:.4f} against {wl['brier_reason']:.4f}), so most of the list's value comes "
                "from dollar amounts. ")
    lim += ("Patient payments are not in the data, so patient balances are treated as not collected. Staff hours, appeal "
            "cost, cash need, advance rate and age limits are financial assumptions that the CFO should replace with "
            "Meridian's own figures; the app lets each be changed and the result re-solved.")
    story.append(P(lim))
    story.append(Spacer(1, 6))
    story.append(P("Data labels used: synthetic_record (simulated records and sums over them), model_estimate (estimated by "
                   "a model in this repository), financial_assumption (an assumed business parameter). Generated by "
                   "src/recon_engine/reports/cfo_report.py from the pipeline database and results/*.json.", "note"))
    return story


def build(out_path: Path | str = OUT_PATH, *, conn=None, results_dir: Path = RESULTS_DIR) -> Path:
    """Write the PDF and return its path. Uses ``conn`` or opens ``recon_engine.db.connect()``."""
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import inch
    from reportlab.platypus import BaseDocTemplate, Frame, PageTemplate

    _fonts()
    styles = _styles()
    own = conn is None
    conn = conn or db.connect()
    try:
        facts = gather(conn, results_dir)
    finally:
        if own:
            conn.close()
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    W, H = letter
    lm = rm = 0.95 * inch
    frame = Frame(lm, 0.85 * inch, W - lm - rm, H - 0.85 * inch - 0.95 * inch, id="body", leftPadding=0, rightPadding=0,
                  topPadding=0, bottomPadding=0)
    header_text = "Meridian Health System (fictional): CFO operations report"

    def on_page(c, doc):
        c.saveState()
        c.setFont("CMR", 9)
        c.setFillColor(GREY)
        c.drawRightString(W - rm, H - 0.58 * inch, header_text)
        c.setStrokeColor(RULE)
        c.setLineWidth(0.5)
        c.line(lm, H - 0.66 * inch, W - rm, H - 0.66 * inch)
        c.drawCentredString(W / 2, 0.5 * inch, f"Page {doc.page}")
        c.restoreState()

    doc = BaseDocTemplate(str(out), pagesize=letter, leftMargin=lm, rightMargin=rm, title="CFO operations report",
                          author="receivables-recon-risk-engine (simulated data)",
                          subject=f"Where the cash is stuck, as of {facts['as_of']}")
    doc.addPageTemplates([PageTemplate(id="all", frames=[frame], onPage=on_page)])
    doc.build(_story(facts, styles, W - lm - rm))
    return out


def page_count(path: Path | str) -> int:
    """Number of pages in a PDF (reads the page tree count; no extra dependency)."""
    import re

    data = Path(path).read_bytes()
    counts = [int(m) for m in re.findall(rb"/Type\s*/Pages\b[^>]*?/Count\s+(\d+)", data)]
    if not counts:
        counts = [int(m) for m in re.findall(rb"/Count\s+(\d+)", data)]
    return max(counts) if counts else 0


if __name__ == "__main__":  # pragma: no cover
    print(build())
