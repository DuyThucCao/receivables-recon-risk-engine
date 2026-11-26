"""The five data labels of DESIGN.md section 8 and helpers to stamp outputs with them.

Every table in ``mart``, every figure and every number in a document carries exactly one label.
"""
from __future__ import annotations

from typing import Any

from psycopg import sql

OBSERVED_PUBLIC = "observed_public"
PUBLISHED_AGGREGATE = "published_aggregate"
SYNTHETIC_RECORD = "synthetic_record"
FINANCIAL_ASSUMPTION = "financial_assumption"
MODEL_ESTIMATE = "model_estimate"

LABELS: tuple[str, ...] = (
    OBSERVED_PUBLIC,
    PUBLISHED_AGGREGATE,
    SYNTHETIC_RECORD,
    FINANCIAL_ASSUMPTION,
    MODEL_ESTIMATE,
)

DESCRIPTIONS: dict[str, str] = {
    OBSERVED_PUBLIC: "measured directly in a public source",
    PUBLISHED_AGGREGATE: "an aggregate statistic published by a public body",
    SYNTHETIC_RECORD: "simulated demonstration record (or a count/sum over simulated records)",
    FINANCIAL_ASSUMPTION: "an assumed business parameter",
    MODEL_ESTIMATE: "a quantity estimated by a model in this repository",
}


def check(label: str) -> str:
    """Return ``label`` unchanged if it is one of the five labels, else raise ValueError."""
    if label not in LABELS:
        raise ValueError(f"unknown data label {label!r}; expected one of {LABELS}")
    return label


def stamp(value: Any, label: str, *, source: str | None = None, note: str | None = None) -> dict:
    """Wrap a number (or any JSON-able value) with its label, e.g. for results/<name>.json."""
    out: dict[str, Any] = {"value": value, "label": check(label)}
    if source:
        out["source"] = source
    if note:
        out["note"] = note
    return out


def label_table(conn, table: str, label: str, note: str = "") -> None:
    """Register a mart table's label in ``mart.table_label`` and as a table comment.

    ``table`` is schema-qualified, e.g. ``mart.ar_aging``. Uses the caller's transaction.
    """
    check(label)
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into mart.table_label (table_name, label, note) values (%s, %s, %s)
            on conflict (table_name) do update set label = excluded.label, note = excluded.note
            """,
            (table, label, note),
        )
        # utility statements cannot take bind parameters: compose the literal client-side
        schema, name = table.split(".", 1)
        cur.execute(sql.SQL("comment on table {}.{} is {}").format(
            sql.Identifier(schema), sql.Identifier(name), sql.Literal(f"label={label}. {note}".strip())))
