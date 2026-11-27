#!/usr/bin/env python
"""Build the project dossier PDF from Markdown sources.

    python scripts/build_dossier.py                  # full build
    python scripts/build_dossier.py --refresh-schema # re-read the DB schema first
    python scripts/build_dossier.py --no-numbers     # skip build_docs_numbers.py --apply

Steps:

1. Refresh the quoted numbers: runs ``scripts/build_docs_numbers.py --apply`` so every
   ``<!--n:key-->value<!--/n-->`` marker in ``docs/project_dossier/src/*.md`` holds the value in
   ``results/*.json``.
2. Draw the diagrams into ``docs/project_dossier/figures/``: business conception map, business
   flow, architecture, close procedure and two ERD panels. The ERD is drawn from
   ``docs/project_dossier/schema_snapshot.json``, which ``--refresh-schema`` rewrites from the live
   database (``information_schema``, read-only). Without a snapshot the ERD step is skipped.
3. Convert the Markdown chapters (in file-name order) to LaTeX with a small converter (headings,
   paragraphs, lists, pipe tables, code blocks, images, ``\\( \\)`` / ``\\[ \\]`` math, quotes) and
   expand ``@@table:NAME@@`` directives into tables generated from ``results/*.json``.
4. Compile with Tectonic (XeLaTeX engine, Latin Modern fonts), following the owner's PDF style
   guide: serif Latin Modern, grey running header with a thin rule, navy section headings with a
   rule, booktabs tables, grey "Page N" footer.

Tectonic is found via ``$TECTONIC``, then ``PATH``, then known install locations. On first use it
downloads the LaTeX packages it needs (internet required once).

Math in the sources uses ``\\( ... \\)`` and ``\\[ ... \\]`` so that a dollar sign is always money.
HTML comments in the sources are dropped from the PDF. Writes ``project_dossier.pdf``.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOSSIER = ROOT / "docs" / "project_dossier"
SRC = DOSSIER / "src"
FIG = DOSSIER / "figures"
BUILD = DOSSIER / "build"
SNAPSHOT = DOSSIER / "schema_snapshot.json"
PDF_OUT = DOSSIER / "project_dossier.pdf"
RESULTS = ROOT / "results"

TECTONIC_CANDIDATES = [
    "/opt/homebrew/bin/tectonic",
    "/usr/local/bin/tectonic",
    str(Path.home() / ".local/bin/tectonic"),
    str(Path.home() / ".cargo/bin/tectonic"),
]

NAVY = "#1F3A68"
LIGHT = "#E8EEF7"
MID = "#5B7DB1"
GREY = "#666666"


# =========================================================================================
# 1. schema snapshot (read-only)
# =========================================================================================
def refresh_schema() -> None:
    sys.path.insert(0, str(ROOT / "src"))
    from recon_engine import db  # noqa: PLC0415

    snap: dict = {"tables": {}, "foreign_keys": []}
    with db.connect() as c:
        c.execute("set default_transaction_read_only = on")
        pk: dict = {}
        for s, t, col in c.execute(
            """select tc.table_schema, tc.table_name, kcu.column_name
               from information_schema.table_constraints tc
               join information_schema.key_column_usage kcu
                 on tc.constraint_name = kcu.constraint_name and tc.table_schema = kcu.table_schema
               where tc.constraint_type = 'PRIMARY KEY' order by kcu.ordinal_position""").fetchall():
            pk.setdefault(f"{s}.{t}", []).append(col)
        for s, t, col, dt in c.execute(
            """select table_schema, table_name, column_name, data_type from information_schema.columns
               where table_schema in ('raw','stg','core','recon','mart')
               order by table_schema, table_name, ordinal_position""").fetchall():
            key = f"{s}.{t}"
            snap["tables"].setdefault(key, {"columns": [], "pk": pk.get(key, [])})
            snap["tables"][key]["columns"].append([col, dt])
        for key in snap["tables"]:
            s, t = key.split(".")
            snap["tables"][key]["rows"] = c.execute(f'select count(*) from "{s}"."{t}"').fetchone()[0]
        for row in c.execute(
            """select tc.table_schema||'.'||tc.table_name, kcu.column_name,
                      ccu.table_schema||'.'||ccu.table_name, ccu.column_name
               from information_schema.table_constraints tc
               join information_schema.key_column_usage kcu
                 on tc.constraint_name = kcu.constraint_name and tc.table_schema = kcu.table_schema
               join information_schema.constraint_column_usage ccu
                 on ccu.constraint_name = tc.constraint_name and ccu.table_schema = tc.table_schema
               where tc.constraint_type = 'FOREIGN KEY'""").fetchall():
            snap["foreign_keys"].append(list(row))
    SNAPSHOT.write_text(json.dumps(snap, indent=1))
    print(f"schema snapshot: {len(snap['tables'])} tables -> {SNAPSHOT.relative_to(ROOT)}")


# =========================================================================================
# 2. diagrams
# =========================================================================================
def _plt():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update({"font.family": "serif", "font.serif": ["DejaVu Serif"], "font.size": 9})
    return plt


def _box(ax, x, y, w, h, text, fc=LIGHT, ec=NAVY, fs=8.5, bold=False, tc="black"):
    from matplotlib.patches import FancyBboxPatch

    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.02,rounding_size=0.08",
                                fc=fc, ec=ec, lw=1.0))
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center", fontsize=fs, color=tc,
            fontweight="bold" if bold else "normal", wrap=True)


def _arrow(ax, x1, y1, x2, y2, text=None, color=NAVY, style="-|>", ls="-", fs=7.5, off=(0, 0.12)):
    ax.annotate("", xy=(x2, y2), xytext=(x1, y1),
                arrowprops=dict(arrowstyle=style, color=color, lw=1.0, linestyle=ls, shrinkA=2, shrinkB=2))
    if text:
        ax.text((x1 + x2) / 2 + off[0], (y1 + y2) / 2 + off[1], text, ha="center", va="bottom",
                fontsize=fs, color=GREY, style="italic")


def fig_conception_map(path: Path) -> None:
    plt = _plt()
    fig, ax = plt.subplots(figsize=(9.5, 7.0))
    ax.set_xlim(0, 10); ax.set_ylim(-1.0, 7.2); ax.axis("off")
    _box(ax, 3.6, 3.0, 2.8, 1.1, "Receivable\n(money owed for a service)", fc=NAVY, ec=NAVY, tc="white", bold=True, fs=10)
    parties = [
        (0.2, 5.6, "Hospital\nbills, posts cash,\nworks denials"),
        (3.6, 5.8, "Payer (insurer)\nadjudicates, remits,\ndenies, recoups"),
        (7.0, 5.6, "Bank\nreceives EFT and\nlockbox deposits"),
        (7.0, 0.5, "Lender\nadvances against\nreceivables"),
        (3.6, 0.3, "Drug wholesaler\nsells lots with\nexpiry dates"),
        (0.2, 0.5, "Patient\nowes deductible\nand coinsurance"),
    ]
    for x, y, t in parties:
        _box(ax, x, y, 2.8, 1.0, t, fs=8.5)
    _arrow(ax, 1.6, 5.6, 4.0, 4.1, "claim (837)", off=(-0.5, 0))
    _arrow(ax, 5.0, 5.8, 5.0, 4.1, "remittance (835)", off=(0.9, 0))
    _arrow(ax, 8.4, 5.6, 6.0, 4.1, "deposit (trace no.)", off=(0.6, 0))
    _arrow(ax, 6.2, 3.0, 8.0, 1.5, "borrowing base", off=(0.6, -0.1))
    _arrow(ax, 5.0, 1.3, 5.0, 3.0, "drug cost billed\non a claim line", off=(1.0, -0.3))
    _arrow(ax, 3.8, 3.0, 1.8, 1.5, "patient share", off=(-0.6, -0.1))
    ax.text(5.0, 7.15, "Who touches one receivable, and which document each one produces",
            ha="center", va="top", fontsize=10, color=NAVY, fontweight="bold")
    qs = ["Q1 CFO: where is cash stuck?", "Q2 Rev-cycle lead: which denials first?",
          "Q3 Treasurer: 13-week cash risk", "Q4 Actuary: unpaid claims reserve",
          "Q5 Credit analyst: borrowing base", "Q6 Pharmacy: drug margin",
          "Q7 Economist: payer underpayment"]
    ax.text(5.0, -0.35, "  |  ".join(qs[:4]), ha="center", fontsize=7.4, color=NAVY)
    ax.text(5.0, -0.75, "  |  ".join(qs[4:]), ha="center", fontsize=7.4, color=NAVY)
    fig.tight_layout(); fig.savefig(path, dpi=200); plt.close(fig)


def fig_business_flow(path: Path) -> None:
    plt = _plt()
    fig, ax = plt.subplots(figsize=(10, 4.8))
    ax.set_xlim(0, 12); ax.set_ylim(0, 5.6); ax.axis("off")
    top = [(0.1, "Service\ndelivered"), (2.0, "Claim submitted\nclaims_export.csv"),
           (4.2, "Payer adjudicates\nclaim_status_history.csv"), (6.6, "Remittance advice\nremittance_advice.csv"),
           (9.1, "Bank deposit\nbank_statement.csv")]
    for x, t in top:
        _box(ax, x, 3.6, 2.0 if x < 1 else 2.2, 1.1, t, fs=8)
    for (x1, _), (x2, _) in zip(top[:-1], top[1:]):
        _arrow(ax, x1 + (2.0 if x1 < 1 else 2.2), 4.15, x2, 4.15)
    _box(ax, 4.2, 1.7, 2.2, 1.0, "Denial work\nappeal / correct / write off", fs=8, fc="#FBEFE6", ec="#C0602A")
    _arrow(ax, 7.6, 3.6, 6.4, 2.4, "denied lines", off=(0.3, -0.05))
    _arrow(ax, 5.3, 2.7, 5.3, 3.6, "resubmit", off=(-0.45, -0.1))
    _box(ax, 2.0, 1.7, 2.0, 1.0, "Contract rates\npayer_contracts.csv", fs=8)
    _arrow(ax, 4.0, 2.4, 4.6, 3.6, "", ls="--")
    _box(ax, 0.1, 0.2, 2.6, 1.0, "Wholesaler invoice\ndrug_purchase_invoices.csv", fs=8)
    _box(ax, 3.2, 0.2, 2.6, 1.0, "Pharmacy stock\n(lot, expiry)", fs=8)
    _box(ax, 6.3, 0.2, 2.6, 1.0, "Administered\ndrug_dispense_log.csv", fs=8)
    _arrow(ax, 2.7, 0.7, 3.2, 0.7); _arrow(ax, 5.8, 0.7, 6.3, 0.7)
    _arrow(ax, 7.6, 1.2, 3.2, 3.6, "billed on a claim line", ls="--", off=(1.6, -0.6))
    _box(ax, 9.4, 1.7, 2.4, 1.0, "Cash posting\nmatch, post, queue", fs=8, fc="#EAF4EA", ec="#3C7D3C")
    _arrow(ax, 7.9, 3.6, 9.9, 2.7, "", ls="--"); _arrow(ax, 10.2, 3.6, 10.6, 2.7, "", ls="--")
    ax.text(6, 5.45, "Business flow: documents (file names) and where money can be lost at each arrow",
            ha="center", fontsize=10, color=NAVY, fontweight="bold")
    fig.tight_layout(); fig.savefig(path, dpi=200); plt.close(fig)


def fig_architecture(path: Path) -> None:
    plt = _plt()
    fig, ax = plt.subplots(figsize=(10, 2.9))
    ax.set_xlim(0, 14); ax.set_ylim(0, 3.2); ax.axis("off")
    layers = [("raw", "7 flat files\nall text\n+ file, row no."), ("stg", "typed, trimmed,\nnormalised IDs,\nrejects with reason"),
              ("core", "facts and dims\nper business\nprocess"), ("recon", "rules as data,\nbalances, queue,\ncontrols, close_day"),
              ("mart", "one output\nper question"), ("models", "Markov, cash,\ncredit, queue, reserve,\ndrugs, economics"),
              ("app / report", "Streamlit screens,\nCFO PDF report")]
    for i, (name, txt) in enumerate(layers):
        x = 0.1 + i * 2.0
        _box(ax, x, 1.75, 1.7, 0.75, name, fc=NAVY, ec=NAVY, tc="white", bold=True, fs=9)
        ax.text(x + 0.85, 1.6, txt, ha="center", va="top", fontsize=7.4, color="black")
        if i:
            _arrow(ax, x - 0.3, 2.12, x, 2.12)
    ax.text(0.1, 2.85, "SQL (PostgreSQL 16, sql/ in numbered order)", fontsize=8.5, color=NAVY, fontweight="bold")
    ax.text(10.1, 2.85, "Python (src/recon_engine, app/)", fontsize=8.5, color=NAVY, fontweight="bold")
    ax.plot([9.95, 9.95], [0.2, 3.0], color=GREY, lw=0.6, ls=":")
    fig.tight_layout(); fig.savefig(path, dpi=200); plt.close(fig)


def fig_close_day(path: Path) -> None:
    plt = _plt()
    fig, ax = plt.subplots(figsize=(10, 3.6))
    ax.set_xlim(0, 12); ax.set_ylim(0, 4); ax.axis("off")
    steps = ["previous day\nclosed?", "daily_recon\nfresh?", "statement chains\nrow by row?",
             "deposits and payments\nfully allocated?", "remittance side\nties?", "unmatched items\nall queued?",
             "posted lines satisfy\nthe identity?"]
    for i, s in enumerate(steps):
        x = 0.05 + i * 1.7
        _box(ax, x, 2.2, 1.5, 1.0, s, fs=7.2)
        if i:
            _arrow(ax, x - 0.2, 2.7, x, 2.7)
        _arrow(ax, x + 0.75, 2.2, x + 0.75, 1.45, "no", color="#B03A2E", off=(0.17, -0.05))
    _box(ax, 0.05, 0.55, 11.7, 0.85, "RAISE: refuse to close, write nothing (tests corrupt one cent in each place)",
         fc="#F7E3E1", ec="#B03A2E", fs=8.5)
    ax.text(11.85, 3.45, "all yes -> mark day CLOSED, log, store numbers hash", ha="right", fontsize=8.5,
            color="#3C7D3C", fontweight="bold")
    ax.text(0.05, 3.75, "recon.close_day(d): every check recomputes from base tables", fontsize=10,
            color=NAVY, fontweight="bold")
    fig.tight_layout(); fig.savefig(path, dpi=200); plt.close(fig)


# ---------------------------------------------------------------- ERD
ERD_PANELS = {
    "erd_core": {
        "title": "Core data model: one model per business process (core schema)",
        "size": (11, 8.2),
        "pos": {  # top-left corner in axis units (0..22 x 0..16.6); four columns, four rows
            "core.dim_service_line": (0.2, 15.6), "core.dim_procedure": (5.75, 15.6),
            "core.dim_payer": (11.3, 15.6), "core.dim_contract_rate": (16.85, 15.6),
            "core.fact_claim_event": (0.2, 11.2), "core.fact_claim_line": (5.75, 11.6),
            "core.fact_remit_line": (11.3, 11.6), "core.fact_remit_adjustment": (16.85, 11.6),
            "core.fact_bank_txn": (0.2, 6.4), "core.fact_drug_dispense": (5.75, 6.4),
            "core.fact_drug_purchase": (11.3, 6.4), "core.dim_reason_code": (16.85, 6.4),
            "core.dim_drug": (8.525, 2.2),
        },
    },
    "erd_recon": {
        "title": "Reconciliation layer (recon schema) and the core tables it links",
        "size": (11, 8.2),
        "pos": {
            "core.fact_claim_line": (0.2, 15.6), "core.fact_remit_line": (5.75, 15.6),
            "recon.remit_trace": (11.3, 15.6), "core.fact_bank_txn": (16.85, 15.6),
            "recon.claim_line_balance": (0.2, 11.0), "recon.claim_remit_match": (5.75, 11.0),
            "recon.remit_duplicate": (11.3, 11.0), "recon.remit_bank_match": (16.85, 11.0),
            "recon.claim_line_status": (0.2, 6.6), "recon.reversal_link": (5.75, 6.6),
            "recon.match_rule": (11.3, 6.6), "recon.exception_queue": (16.85, 6.6),
            "recon.control_totals": (5.75, 2.4), "recon.daily_recon": (11.3, 2.4),
        },
    },
}
# logical relationships (child table.column -> parent table.column); drawn only if both exist
ERD_LINKS = [
    ("core.fact_claim_line.payer_id", "core.dim_payer.payer_id"),
    ("core.fact_claim_line.procedure_code", "core.dim_procedure.procedure_code"),
    ("core.fact_claim_line.service_line_code", "core.dim_service_line.service_line_code"),
    ("core.dim_procedure.service_line_code", "core.dim_service_line.service_line_code"),
    ("core.dim_contract_rate.payer_id", "core.dim_payer.payer_id"),
    ("core.fact_remit_line.payer_id", "core.dim_payer.payer_id"),
    ("core.fact_bank_txn.payer_id_inferred", "core.dim_payer.payer_id"),
    ("core.fact_remit_adjustment.remit_line_id", "core.fact_remit_line.remit_line_id"),
    ("core.fact_remit_adjustment.reason_code", "core.dim_reason_code.reason_code"),
    ("core.fact_claim_event.claim_id", "core.fact_claim_line.claim_id"),
    ("core.fact_drug_dispense.claim_line_id", "core.fact_claim_line.claim_line_id"),
    ("core.fact_drug_dispense.hcpcs_code", "core.dim_drug.hcpcs_code"),
    ("core.fact_drug_purchase.hcpcs_code", "core.dim_drug.hcpcs_code"),
    ("core.fact_drug_dispense.lot_no", "core.fact_drug_purchase.lot_no"),
    ("recon.claim_remit_match.claim_line_id", "core.fact_claim_line.claim_line_id"),
    ("recon.claim_remit_match.remit_line_id", "core.fact_remit_line.remit_line_id"),
    ("recon.claim_remit_match.rule_id", "recon.match_rule.rule_id"),
    ("recon.remit_trace.trace_key", "core.fact_remit_line.trace_key"),
    ("recon.remit_bank_match.trace_key", "recon.remit_trace.trace_key"),
    ("recon.remit_bank_match.bank_txn_id", "core.fact_bank_txn.bank_txn_id"),
    ("recon.remit_bank_match.rule_id", "recon.match_rule.rule_id"),
    ("recon.remit_duplicate.remit_line_id", "core.fact_remit_line.remit_line_id"),
    ("recon.reversal_link.reversal_remit_line_id", "core.fact_remit_line.remit_line_id"),
    ("recon.reversal_link.claim_line_id", "core.fact_claim_line.claim_line_id"),
    ("recon.claim_line_balance.claim_line_id", "core.fact_claim_line.claim_line_id"),
    ("recon.claim_line_status.claim_line_id", "recon.claim_line_balance.claim_line_id"),
    ("recon.exception_queue.claim_line_id", "recon.claim_line_balance.claim_line_id"),
    ("recon.exception_queue.bank_txn_id", "core.fact_bank_txn.bank_txn_id"),
    ("recon.exception_queue.trace_key", "recon.remit_trace.trace_key"),
]


def fig_erd(name: str, snap: dict, path: Path) -> list[str]:
    plt = _plt()
    from matplotlib.patches import Rectangle

    panel = ERD_PANELS[name]
    pos = panel["pos"]
    tables = snap["tables"]
    fig, ax = plt.subplots(figsize=panel["size"])
    ax.set_xlim(0, 22); ax.set_ylim(0, 16.6); ax.axis("off")
    link_cols: dict[str, set] = {}
    for child, parent in ERD_LINKS:
        for ref in (child, parent):
            t, c = ref.rsplit(".", 1)
            link_cols.setdefault(t, set()).add(c)
    geom = {}
    missing = []
    W, LH = 4.95, 0.36
    for t, (x, y) in pos.items():
        if t not in tables:
            missing.append(t)
            continue
        info = tables[t]
        cols = [c for c, _ in info["columns"]]
        show = [c for c in cols if c in info["pk"] or c in link_cols.get(t, set())]
        extra = len(cols) - len(show)
        lines = [("PK " if c in info["pk"] else "     ") + c for c in show]
        if extra:
            lines.append(f"     + {extra} more columns")
        h = LH * (len(lines) + 1.2)
        ax.add_patch(Rectangle((x, y - h), W, h, fc="white", ec=NAVY, lw=1.0))
        ax.add_patch(Rectangle((x, y - LH * 1.2), W, LH * 1.2, fc=NAVY, ec=NAVY, lw=1.0))
        ax.text(x + 0.12, y - LH * 0.6, f"{t}", color="white", fontsize=7.2, va="center", fontweight="bold")
        ax.text(x + W - 0.1, y - LH * 0.6, f"{info.get('rows', 0):,} rows", color="#D6E0F0",
                fontsize=5.8, va="center", ha="right")
        rowy = {}
        for i, ln in enumerate(lines):
            yy = y - LH * (1.2 + i + 0.55)
            ax.text(x + 0.12, yy, ln, fontsize=6.6, va="center", family="monospace",
                    color="black" if not ln.startswith("     +") else GREY)
            rowy[ln.strip().replace("PK ", "")] = yy
        geom[t] = (x, y, W, h, rowy)
    for child, parent in ERD_LINKS:
        ct, cc = child.rsplit(".", 1)
        pt, pc = parent.rsplit(".", 1)
        if ct not in geom or pt not in geom:
            continue
        cx, cy, cw, ch, crow = geom[ct]
        px, py, pw, ph, prow = geom[pt]
        y1 = crow.get(cc, cy - ch / 2)
        y2 = prow.get(pc, py - ph / 2)
        x1 = cx + cw if px > cx + cw / 2 else cx
        x2 = px if px > cx + cw / 2 else px + pw
        if abs(px - cx) < 1.0:   # stacked vertically
            x1 = cx + cw * 0.5; x2 = px + pw * 0.5
            y1 = cy if py > cy else cy - ch
            y2 = py - ph if py > cy else py
        ax.plot([x1, x2], [y1, y2], color=MID, lw=0.8, alpha=0.85)
        ax.plot([x1], [y1], marker="o", ms=3, color=MID)          # many side
        ax.plot([x2], [y2], marker="|", ms=7, mew=1.4, color=NAVY)  # one side
    ax.text(0.1, 16.5, panel["title"], fontsize=10, color=NAVY, fontweight="bold", va="top")
    ax.text(21.9, 0.05, "Drawn from information_schema. o = many side, | = one side. "
            "PK = primary key; other listed columns are join keys.", fontsize=6.6, color=GREY, ha="right")
    fig.tight_layout(); fig.savefig(path, dpi=200); plt.close(fig)
    return missing


def make_figures(snap: dict | None) -> None:
    FIG.mkdir(parents=True, exist_ok=True)
    fig_conception_map(FIG / "conception_map.png")
    fig_business_flow(FIG / "business_flow.png")
    fig_architecture(FIG / "architecture.png")
    fig_close_day(FIG / "close_day.png")
    if snap:
        for name in ERD_PANELS:
            miss = fig_erd(name, snap, FIG / f"{name}.png")
            if miss:
                print(f"ERD {name}: tables not in snapshot, skipped: {miss}")
    else:
        print("no schema snapshot: ERD figures not drawn (run with --refresh-schema)")


# =========================================================================================
# 3. tables generated from results/*.json
# =========================================================================================
def _load(name: str):
    p = RESULTS / name
    return json.loads(p.read_text()) if p.exists() else None


def _m(v, nd=2):
    return f"{v:,.{nd}f}"


def gen_table(name: str) -> str:
    """Return Markdown pipe-table text for a directive, or a visible placeholder."""
    if name == "control_totals":
        d = _load("control_totals.json")
        if not d:
            return "_{{control_totals.json missing}}_"
        rows = ["| Check | Scope | Expected | Difference | Passed |", "|---|---|---:|---:|---|"]
        for c in d["checks"]:
            rows.append(f"| `{c['check_name']}` | {c['scope']} | {_m(c['expected'])} | {_m(c['difference'])} | "
                        f"{'yes' if c['passed'] else 'NO'} |")
        return "\n".join(rows)
    if name == "exceptions":
        d = _load("reconciliation.json")
        if not d:
            return "_{{reconciliation.json missing}}_"
        rows = ["| Exception type | Severity | Open items | Amount |", "|---|---|---:|---:|"]
        for r in d["open_exceptions"]:
            rows.append(f"| `{r['exception_type']}` | {r['severity']} | {r['n']:,} | {_m(r['amount'])} |")
        return "\n".join(rows)
    if name == "cash_stuck":
        d = _load("core_build.json")
        if not d:
            return "_{{core_build.json missing}}_"
        rows = ["| Bucket | Items | Amount |", "|---|---:|---:|"]
        for r in d["cash_stuck"]:
            rows.append(f"| `{r['bucket']}` | {r['n_items']:,} | {_m(r['amount'])} |")
        return "\n".join(rows)
    if name in ("claim_cases", "bank_cases"):
        d = _load("reconciliation.json")
        if not d:
            return "_{{reconciliation.json missing}}_"
        part = d["claim_to_remittance" if name == "claim_cases" else "remittance_to_bank"]["by_case"]
        unit = "Lines" if name == "claim_cases" else "Payments"
        rows = [f"| Case | {unit} | Correct | Wrong | Queued | Recall |", "|---|---:|---:|---:|---:|---:|"]
        for k, v in part.items():
            n = v.get("lines", v.get("payments", 0))
            if "matched_correct" not in v:
                rows.append(f"| {k.replace('_', ' ')} (should stay unmatched) | {n:,} | "
                            f"{v.get('correctly_left_unmatched', 0):,} left unmatched | {v.get('wrongly_matched', 0)} | - | - |")
                continue
            rec = v.get("recall")
            rows.append(f"| {k.replace('_', ' ')} | {n:,} | {v['matched_correct']:,} | {v['matched_wrong']} | "
                        f"{v.get('sent_to_exception_queue', 0):,} | {'-' if rec is None else f'{rec:.3f}'} |")
        return "\n".join(rows)
    if name == "rules":
        d = _load("reconciliation.json")
        if not d:
            return "_{{reconciliation.json missing}}_"
        rows = ["| Rule | Matched | Wrong | Precision |", "|---|---:|---:|---:|"]
        for k, v in d["claim_to_remittance"]["per_rule"].items():
            rows.append(f"| `{k}` | {v['matched']:,} lines | {v['wrong']} | {v['precision']:.4f} |")
        for k, v in d["remittance_to_bank"]["per_rule"].items():
            rows.append(f"| `{k}` | {v['payments_matched']:,} payments | {v['wrong']} | {v['precision']:.4f} |")
        return "\n".join(rows)
    if name == "markov_matrix":
        d = _load("markov.json")
        if not d:
            return "_{{markov.json missing}}_"
        m = d["per_payer"]["ALL"]["matrix"]["value"]
        states = d["states"]
        short = {"SUBMITTED": "SUB", "IN_REVIEW": "REV", "PENDED": "PEND", "DENIED": "DEN",
                 "APPEALED": "APP", "PAID": "PAID", "WRITTEN_OFF": "WO"}
        rows = ["| from \\ to | " + " | ".join(short[s] for s in states) + " |",
                "|---|" + "---:|" * len(states)]
        for a in m:
            rows.append(f"| {short[a]} | " + " | ".join(f"{m[a][b]:.3f}" if m[a][b] else "." for b in states) + " |")
        return "\n".join(rows)
    if name == "credit_waterfall":
        d = _load("credit.json")
        if not d:
            return "_{{credit.json missing}}_"
        t = d["certificate_steady_state"]["value"]["totals"]
        labels = [("gross_ncv", "Gross net collectible value"), ("inel_denied", "less: denied"),
                  ("inel_disputed", "less: disputed (appealed or pended)"), ("inel_aged", "less: aged beyond limit"),
                  ("inel_cross_aged", "less: cross-aged payers"), ("concentration_excess", "less: concentration excess"),
                  ("eligible", "Eligible"), ("credit_balance_reserve", "less: credit-balance reserve"),
                  ("availability", "Availability (advance rate x eligible - reserve)"),
                  ("expected_loss", "Expected loss on eligible pool")]
        rows = ["| Line | Amount |", "|---|---:|"]
        for k, lab in labels:
            if k in t:
                rows.append(f"| {lab} | {_m(t[k], 0)} |")
        return "\n".join(rows)
    if name == "credit_stress":
        d = _load("credit.json")
        if not d:
            return "_{{credit.json missing}}_"
        rows = ["| Scenario | Rate | Availability | Interest, annual | Carry cost of delay |", "|---|---:|---:|---:|---:|"]
        for r in d["stress_steady_state"]["value"]:
            rows.append(f"| {r['scenario']} | {r['rate_annual'] * 100:.2f}% | {_m(r['availability'], 0)} | "
                        f"{_m(r['interest_cost_annual'], 0)} | {_m(r['carry_cost_of_delay'], 0)} |")
        return "\n".join(rows)
    if name == "econ_payers":
        d = _load("econ.json")
        if not d:
            return "_{{econ.json missing}}_"
        inc = {r["payer_id"]: r for r in d["incentives_by_payer"]["rows"]}
        rows = ["| Payer | Claims underpaid | Shortfall rate (95% CI) | Shortfall $ | Appeal threshold |",
                "|---|---:|---:|---:|---:|"]
        for r in sorted(d["underpayment_by_payer"]["rows"], key=lambda r: -r["shortfall_rate"]):
            p = r["payer_id"]
            rows.append(f"| {p} | {r['underpaid_claim_share'] * 100:.1f}% | {r['shortfall_rate'] * 100:.2f}% "
                        f"({r['shortfall_rate_lo'] * 100:.2f}-{r['shortfall_rate_hi'] * 100:.2f}%) | "
                        f"{_m(r['shortfall_usd'])} | {_m(inc[p]['threshold_usd'])} |")
        return "\n".join(rows)
    if name == "reserve_retro":
        d = _load("reserve.json")
        if not d:
            return "_{{reserve.json missing}}_"
        rows = ["| Earlier as-of | CL estimate | BF estimate | Paid afterwards | CL / observed | BF / observed | Inside 90% |",
                "|---|---:|---:|---:|---:|---:|---|"]
        for r in d["retrospective"]["rows"]:
            rows.append(f"| {r['retro_as_of']} | {_m(r['unpaid_cl'], 0)} | {_m(r['unpaid_bf'], 0)} | "
                        f"{_m(r['paid_later_observed'], 0)} | {r['ratio_estimate_to_observed']:.2f} | "
                        f"{r['ratio_bf_to_observed']:.2f} | {'yes' if r['observed_inside_90pct_interval'] else 'no'} |")
        return "\n".join(rows)
    if name == "q2_metrics":
        p, e = _load("prioritize.json"), _load("prioritize_evaluation.json")
        if not p or not e:
            return "_{{prioritize results missing}}_"
        h = p["validation"]["held_out_metrics"]
        rows = ["| Held-out appeals | Brier | Log loss | AUC |", "|---|---:|---:|---:|"]
        for k, lab in [("logistic_model", "Logistic model"), ("baseline_overall_rate", "Baseline: overall rate"),
                       ("baseline_reason_rate", "Baseline: reason-category rate")]:
            rows.append(f"| {lab} | {h[k]['brier']:.4f} | {h[k]['log_loss']:.4f} | {h[k]['auc']:.4f} |")
        pm = e["planted_probability_metrics_on_test"]
        rows.append(f"| Generator's own probabilities (ceiling) | {pm['brier']:.4f} | {pm['log_loss']:.4f} | {pm['auc']:.4f} |")
        return "\n".join(rows)
    if name == "q2_backtest":
        p, e = _load("prioritize.json"), _load("prioritize_evaluation.json")
        if not p or not e:
            return "_{{prioritize results missing}}_"
        pl = {r["hours_per_day"]: r for r in e["backtest_value_under_planted_probabilities"]}
        rows = ["| Hours/day | Knapsack (planted p) | Largest first | Oldest first | Realised: knapsack | Realised: oldest |",
                "|---:|---:|---:|---:|---:|---:|"]
        rz = {r["hours_per_day"]: r for r in p["backtest"]["realised_net_appealed_with_outcome"]}
        for h in sorted(pl):
            if h not in (0.75, 1.0, 2.0, 6.0):
                continue
            r = pl[h]
            rows.append(f"| {h:g} | {_m(r['model_knapsack'], 0)} | {_m(r['largest_first'], 0)} | {_m(r['oldest_first'], 0)} | "
                        f"{_m(rz[h]['model_knapsack'], 0)} | {_m(rz[h]['oldest_first'], 0)} |")
        return "\n".join(rows)
    if name == "drug_losers":
        d = _load("drug.json")
        if not d:
            return "_{{drug.json missing}}_"
        rows = ["| HCPCS | Margin | Denied lines | Main driver |", "|---|---:|---:|---|"]
        for r in d["drugs_mature"]["rows"][:8]:
            rows.append(f"| {r['hcpcs']} | {_m(r['margin_collected'])} | {r['denied_line_share'] * 100:.0f}% | {r['primary_driver']} |")
        return "\n".join(rows)
    if name == "tests":
        rows = ["| Test file | Tests | What it proves (examples) |", "|---|---:|---|"]
        for f in sorted((ROOT / "tests").glob("test_*.py")):
            names = re.findall(r"^def (test_\w+)", f.read_text(), re.M)
            ex = ", ".join(n.replace("test_", "").replace("_", " ") for n in names[:3])
            rows.append(f"| `{f.name}` | {len(names)} | {ex} |")
        return "\n".join(rows)
    if name == "numbers":
        p = ROOT / "docs" / "_numbers.json"
        if not p.exists():
            return "_{{docs/_numbers.json missing}}_"
        nums = json.loads(p.read_text())
        ab = {"observed_public": "OP", "published_aggregate": "PA", "synthetic_record": "SR",
              "financial_assumption": "FA", "model_estimate": "ME", "n/a (code fact)": "code"}
        items = [(k, v["value"], ab.get(v.get("label") or "", "-")) for k, v in nums.items()]
        half = (len(items) + 1) // 2
        left, right = items[:half], items[half:] + [("", "", "")]
        rows = ["| Key | Value | L | Key | Value | L |", "|---|---|---|---|---|---|"]
        for (k1, v1, l1), (k2, v2, l2) in zip(left, right):
            c2 = f"`{k2}`" if k2 else ""
            rows.append(f"| `{k1}` | {v1} | {l1} | {c2} | {v2} | {l2} |")
        return "\n".join(rows)
    return f"_{{{{unknown table {name}}}}}_"


# =========================================================================================
# 4. Markdown -> LaTeX
# =========================================================================================
_ESC = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_",
        "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}


def esc(s: str) -> str:
    s = "".join(_ESC.get(ch, ch) for ch in s)
    s = s.replace("<=", r"$\le$").replace(">=", r"$\ge$")
    s = s.replace("->", r"$\rightarrow$").replace("<-", r"$\leftarrow$")
    s = s.replace("\u2013", "--").replace("\u2014", "---")
    return s


def code_esc(s: str) -> str:
    out = esc(s)
    out = out.replace(r"\_", r"\_\allowbreak{}").replace(".", r".\allowbreak{}").replace("/", r"/\allowbreak{}")
    return out


def inline(s: str) -> str:
    """Inline Markdown to LaTeX, protecting math and code spans."""
    s = re.sub(r"<!--n:[^>]*-->(.*?)<!--/n-->", r"\1", s)
    s = re.sub(r"<!--.*?-->", "", s)
    tokens = re.split(r"(\\\(.*?\\\)|`[^`]+`|\{\{[^}]+\}\})", s)
    out = []
    for t in tokens:
        if not t:
            continue
        if t.startswith("\\(") and t.endswith("\\)"):
            out.append(t)
        elif t.startswith("`"):
            out.append(r"\code{" + code_esc(t[1:-1]) + "}")
        elif t.startswith("{{"):
            out.append(r"\placeholder{" + esc(t) + "}")
        else:
            x = esc(t)
            x = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", lambda m: m.group(1), x)
            x = re.sub(r"\*\*(.+?)\*\*", r"\\textbf{\1}", x)
            x = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\\emph{\1}", x)
            out.append(x)
    return "".join(out)


def table_tex(lines: list[str]) -> str:
    def cells(l: str) -> list[str]:
        l = l.strip().strip("|")
        return [c.strip() for c in re.split(r"(?<!\\)\|", l)]

    head = cells(lines[0])
    align = cells(lines[1])
    body = [cells(l) for l in lines[2:]]
    n = len(head)
    lens = [max([len(re.sub(r"<!--.*?-->", "", head[i]))] + [len(re.sub(r"<!--.*?-->", "", r[i])) if i < len(r) else 0 for r in body]) for i in range(n)]
    lens = [min(max(l, 4), 60) for l in lens]
    tot = sum(lens)
    numeric = [a.endswith(":") and not a.startswith(":") for a in align]
    specs = []
    for i in range(n):
        w = 0.97 * lens[i] / tot
        al = r"\raggedleft" if numeric[i] else r"\raggedright"
        specs.append(rf">{{{al}\arraybackslash}}p{{\dimexpr {w:.3f}\linewidth - 8pt\relax}}")
    fs = r"\small" if n <= 4 else (r"\footnotesize" if n <= 5 else r"\scriptsize")
    out = [r"{" + fs + r"\setlength{\tabcolsep}{4pt}\renewcommand{\arraystretch}{1.12}",
           r"\begin{longtable}{@{}" + "".join(specs) + r"@{}}", r"\toprule",
           " & ".join(r"\textbf{" + inline(h) + "}" for h in head) + r" \\", r"\midrule", r"\endhead"]
    for r in body:
        r = (r + [""] * n)[:n]
        out.append(" & ".join(inline(c) for c in r) + r" \\")
    out += [r"\bottomrule", r"\end{longtable}}", ""]
    return "\n".join(out)


def image_tex(alt: str, path: str, width: str | None) -> str:
    # links are written relative to src/ so they also resolve on GitHub
    cands = [Path(path)] if path.startswith("/") else [(SRC / path).resolve(), (DOSSIER / path).resolve(), (ROOT / path).resolve()]
    p = next((c for c in cands if c.exists()), cands[0])
    w = width or "0.92"
    if not p.exists():
        return (r"\begin{center}\fbox{\parbox{0.86\linewidth}{\centering\vspace{1.2em}\textcolor{gray}{"
                r"Figure slot: \code{" + code_esc(path) + r"} is not available yet. " + esc(alt) +
                r"}\vspace{1.2em}}}\end{center}")
    return (r"\begin{figure}[H]\centering\includegraphics[width=" + w + r"\linewidth]{" + str(p) + "}"
            r"\caption{" + inline(alt) + r"}\end{figure}")


def md_to_tex(md: str) -> str:
    # directives
    md = re.sub(r"^@@table:(\w+)@@\s*$", lambda m: gen_table(m.group(1)), md, flags=re.M)
    lines = md.splitlines()
    out: list[str] = []
    i = 0
    para: list[str] = []

    def flush():
        if para:
            text = " ".join(x.strip() for x in para)
            out.append(inline(text))
            out.append("")
            para.clear()

    while i < len(lines):
        ln = lines[i]
        s = ln.strip()
        if s.startswith("```"):
            flush()
            j = i + 1
            buf = []
            while j < len(lines) and not lines[j].strip().startswith("```"):
                buf.append(lines[j]); j += 1
            out.append(r"\begin{Verbatim}[fontsize=\footnotesize,breaklines,breakanywhere]")
            out.extend(re.sub(r"<!--n:[^>]*-->(.*?)<!--/n-->", r"\1", b) for b in buf)
            out.append(r"\end{Verbatim}")
            i = j + 1
            continue
        if s.startswith("\\["):
            flush()
            buf = [ln]
            while not lines[i].strip().endswith("\\]"):
                i += 1; buf.append(lines[i])
            out.append("\n".join(buf)); out.append("")
            i += 1
            continue
        if s.startswith("%%"):       # raw LaTeX line
            flush(); out.append(s[2:].strip()); i += 1; continue
        m = re.match(r"^(#{1,4})\s+(.*)$", s)
        if m:
            flush()
            lvl = len(m.group(1))
            cmd = {1: "section", 2: "subsection", 3: "subsubsection", 4: "paragraph"}[lvl]
            title = m.group(2)
            star = "*" if title.endswith("{-}") else ""
            title = title.replace("{-}", "").strip()
            out.append(rf"\{cmd}{star}{{{inline(title)}}}")
            i += 1
            continue
        if s.startswith("|") and i + 1 < len(lines) and re.match(r"^\|?\s*:?-{2,}", lines[i + 1].strip()):
            flush()
            buf = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                buf.append(lines[i]); i += 1
            out.append(table_tex(buf))
            continue
        m = re.match(r"^!\[(.*?)\]\((.*?)\)(\{width=([\d.]+)\})?\s*$", s)
        if m:
            flush(); out.append(image_tex(m.group(1), m.group(2), m.group(4))); i += 1; continue
        if re.match(r"^([-*]|\d+\.)\s+", s):
            flush()
            env = "enumerate" if re.match(r"^\d+\.", s) else "itemize"
            items: list[list] = []          # [text, [subitems]]
            bullet = re.compile(r"^([-*]|\d+\.)\s+")
            while i < len(lines):
                t = lines[i]
                st = t.strip()
                ind = len(t) - len(t.lstrip())
                if not st:
                    break
                if bullet.match(st) and ind < 2:
                    items.append([bullet.sub("", st), []])
                elif bullet.match(st) and items:
                    items[-1][1].append(bullet.sub("", st))
                elif items and ind >= 2:
                    if items[-1][1]:
                        items[-1][1][-1] += " " + st
                    else:
                        items[-1][0] += " " + st
                else:
                    break
                i += 1
            out.append(rf"\begin{{{env}}}[leftmargin=1.6em,itemsep=1pt,topsep=3pt]")
            for text, subs in items:
                out.append(r"\item " + inline(text))
                if subs:
                    out.append(r"\begin{itemize}[leftmargin=1.4em,itemsep=0pt,topsep=1pt]")
                    out.extend(r"\item " + inline(x) for x in subs)
                    out.append(r"\end{itemize}")
            out.append(rf"\end{{{env}}}")
            continue
        if s.startswith(">"):
            flush()
            buf = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip()[1:].strip()); i += 1
            chunks, cur = [], []
            for b in buf:
                if b == "":
                    if cur: chunks.append(" ".join(cur)); cur = []
                else:
                    cur.append(b)
            if cur: chunks.append(" ".join(cur))
            out.append(r"\begin{memo}" + r"\par ".join(inline(c) for c in chunks) + r"\end{memo}")
            continue
        if not s:
            flush(); i += 1; continue
        para.append(ln)
        i += 1
    flush()
    return "\n".join(out)


PREAMBLE = r"""
\documentclass[11pt,letterpaper]{article}
\usepackage[margin=1in,headheight=14pt]{geometry}
\usepackage{fontspec}
\setmainfont{lmroman10-regular.otf}[BoldFont=lmroman10-bold.otf,ItalicFont=lmroman10-italic.otf,BoldItalicFont=lmroman10-bolditalic.otf]
\setmonofont{lmmono10-regular.otf}[Scale=0.95]
\usepackage{amsmath}
\usepackage{unicode-math}
\setmathfont{latinmodern-math.otf}
\usepackage{xcolor,graphicx,float,booktabs,longtable,array,enumitem,fancyhdr,titlesec,caption,fvextra,microtype}
\usepackage[hidelinks]{hyperref}
\definecolor{navy}{HTML}{1F3A68}
\definecolor{lightnavy}{HTML}{4A6FA5}
\setlength{\parindent}{0pt}
\setlength{\parskip}{0.45em}
\linespread{1.04}
\titleformat{\section}{\Large\bfseries\color{navy}}{\thesection}{0.6em}{}[{\vspace{2pt}\color{navy}\titlerule[0.6pt]}]
\titleformat{\subsection}{\large\bfseries\color{lightnavy}}{\thesubsection}{0.6em}{}
\titleformat{\subsubsection}{\normalsize\bfseries\color{lightnavy}}{\thesubsubsection}{0.6em}{}
\titlespacing*{\section}{0pt}{1.4em}{0.8em}
\titlespacing*{\subsection}{0pt}{1.1em}{0.4em}
\pagestyle{fancy}
\fancyhf{}
\fancyhead[R]{\small\color{gray}Receivables Reconciliation \& Risk Engine --- Project dossier}
\renewcommand{\headrulewidth}{0.4pt}
\renewcommand{\headrule}{\hbox to\headwidth{\color{gray}\leaders\hrule height \headrulewidth\hfill}}
\fancyfoot[C]{\small\color{gray}Page \thepage}
\fancypagestyle{plain}{\fancyhf{}\fancyfoot[C]{\small\color{gray}Page \thepage}\renewcommand{\headrulewidth}{0pt}}
\captionsetup{font=small,labelfont={bf,color=navy}}
\newcommand{\code}[1]{\texttt{\small #1}}
\newcommand{\placeholder}[1]{\textcolor{red}{\texttt{#1}}}
\newenvironment{memo}{\par\begingroup\leftskip=1.2em\rightskip=1.2em\small\itshape}{\par\endgroup}
\setlength{\emergencystretch}{1.5em}\setlength{\LTpre}{4pt}\setlength{\LTpost}{6pt}
\begin{document}
"""


def build_tex() -> Path:
    BUILD.mkdir(parents=True, exist_ok=True)
    parts = []
    for f in sorted(SRC.glob("*.md")):
        parts.append(f"% ---- {f.name}\n" + md_to_tex(f.read_text()))
    tex = PREAMBLE + "\n\n".join(parts) + "\n\\end{document}\n"
    p = BUILD / "project_dossier.tex"
    p.write_text(tex)
    return p


def find_tectonic() -> str:
    cands = [os.environ.get("TECTONIC", ""), shutil.which("tectonic") or ""] + TECTONIC_CANDIDATES
    for c in cands:
        if c and Path(c).exists() and os.access(c, os.X_OK):
            return c
    raise SystemExit("Tectonic not found. Set TECTONIC=/path/to/tectonic (https://tectonic-typesetting.github.io).")


def compile_pdf(tex: Path) -> None:
    exe = find_tectonic()
    r = subprocess.run([exe, "-X", "compile", "--keep-logs", tex.name], cwd=tex.parent,
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout[-3000:], r.stderr[-3000:])
        raise SystemExit("LaTeX build failed; see docs/project_dossier/build/project_dossier.log")
    out = PDF_OUT
    shutil.copy(tex.with_suffix(".pdf"), out)
    try:
        from pypdf import PdfReader

        n = len(PdfReader(str(out)).pages)
        print(f"wrote {out.relative_to(ROOT)}: {n} pages")
    except Exception:  # noqa: BLE001
        print(f"wrote {out.relative_to(ROOT)}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the project dossier PDF.")
    ap.add_argument("--refresh-schema", action="store_true", help="re-read the schema from the database (read-only)")
    ap.add_argument("--no-numbers", action="store_true", help="do not run build_docs_numbers.py --apply first")
    ap.add_argument("--tex-only", action="store_true", help="write the .tex but do not compile")
    a = ap.parse_args()
    if not a.no_numbers:
        subprocess.run([sys.executable, str(ROOT / "scripts" / "build_docs_numbers.py"), "--apply"], check=False)
    if a.refresh_schema:
        refresh_schema()
    snap = json.loads(SNAPSHOT.read_text()) if SNAPSHOT.exists() else None
    make_figures(snap)
    tex = build_tex()
    if not a.tex_only:
        compile_pdf(tex)
    return 0


if __name__ == "__main__":
    sys.exit(main())
