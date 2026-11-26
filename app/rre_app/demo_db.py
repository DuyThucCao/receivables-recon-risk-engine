"""The demo database the app reads and the reconciliation desk writes to.

The pipeline builds the *baseline* database (``receivables`` unless ``RRE_DB_NAME`` says
otherwise). The app never writes to it. Instead it works on a copy, ``<baseline>_demo``, made with
``CREATE DATABASE ... TEMPLATE <baseline> STRATEGY FILE_COPY`` (under a second on a laptop). An
intake on the reconciliation desk lands rows in the copy; **Reset** drops the copy and makes it
again from the baseline, so a demo can be repeated exactly.

The copy records the baseline it came from as a database comment (a fingerprint of the last
reconciliation run and the mart tables). When the baseline is rebuilt (``run_all.py`` or a model
script), the next app start notices the fingerprint changed and refreshes the copy.

With ``DATABASE_URL`` set (a hosted Postgres) databases cannot be copied, so the app reads the
baseline directly and Reset is disabled.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import psycopg
from psycopg import sql

from . import paths  # noqa: F401
from recon_engine import db  # noqa: E402

COMMENT_PREFIX = "rre-demo baseline="


class BaselineMissing(RuntimeError):
    """The pipeline has not built the baseline database yet."""


@dataclass
class DemoStatus:
    database: str
    baseline: str
    refreshed: bool
    hosted: bool
    message: str = ""


def hosted() -> bool:
    return bool(os.environ.get("DATABASE_URL"))


def baseline_name() -> str:
    return db.db_name()


def demo_name() -> str:
    return os.environ.get("RRE_DEMO_DB", f"{baseline_name()}_demo")


def _maintenance() -> psycopg.Connection:
    return psycopg.connect(db.server_uri("postgres"), autocommit=True)


def _exists(c: psycopg.Connection, name: str) -> bool:
    return c.execute("select 1 from pg_database where datname = %s", (name,)).fetchone() is not None


def fingerprint(conn: psycopg.Connection) -> str:
    """Identity of a database's built state: last reconciliation run plus the mart tables (oids
    change whenever a model drops and recreates its tables). A template copy has the same value."""
    run = conn.execute("select coalesce(max(run_id)::text, '') || '@' || coalesce(max(started_at)::text, '') "
                       "from recon.run_log").fetchone()[0]
    marts = conn.execute("""select coalesce(string_agg(c.relname || ':' || c.oid::text, ',' order by c.relname), '')
                            from pg_class c join pg_namespace n on n.oid = c.relnamespace
                            where n.nspname = 'mart' and c.relkind = 'r'""").fetchone()[0]
    import hashlib
    return hashlib.sha1(f"{run}|{marts}".encode()).hexdigest()[:16]


def baseline_fingerprint() -> str:
    with _maintenance() as m:
        if not _exists(m, baseline_name()):
            raise BaselineMissing(
                f"database '{baseline_name()}' does not exist on {db.pg_dir()}: run "
                "`python run_all.py` first")
    with psycopg.connect(db.server_uri(baseline_name())) as c:
        try:
            return fingerprint(c)
        except psycopg.errors.UndefinedTable as e:
            raise BaselineMissing(f"database '{baseline_name()}' has no reconciliation tables: run run_all.py") from e


def _demo_comment(m: psycopg.Connection) -> str | None:
    r = m.execute("select shobj_description(oid, 'pg_database') from pg_database where datname = %s",
                  (demo_name(),)).fetchone()
    return r[0] if r else None


def recreate_demo() -> None:
    """Drop the demo copy and make it again from the baseline (the Reset button)."""
    if hosted():
        raise RuntimeError("reset is not available on a hosted DATABASE_URL server")
    fp = baseline_fingerprint()
    with _maintenance() as m:
        m.execute(sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(demo_name())))
        try:
            m.execute(sql.SQL("create database {} template {} strategy file_copy").format(
                sql.Identifier(demo_name()), sql.Identifier(baseline_name())))
        except psycopg.errors.ObjectInUse as e:
            raise RuntimeError(
                f"the baseline database '{baseline_name()}' is in use by another process (a model script or the "
                "report builder?). Wait for it to finish and press Reset again.") from e
        m.execute(sql.SQL("comment on database {} is {}").format(
            sql.Identifier(demo_name()), sql.Literal(COMMENT_PREFIX + fp)))


def ensure_demo() -> DemoStatus:
    """Make sure the demo copy exists and matches the current baseline; return where to connect."""
    if hosted():
        return DemoStatus(baseline_name(), baseline_name(), False, True,
                          "Hosted database: the app reads it directly and the reset button is disabled.")
    fp = baseline_fingerprint()
    with _maintenance() as m:
        current = _demo_comment(m) if _exists(m, demo_name()) else None
    if current == COMMENT_PREFIX + fp:
        return DemoStatus(demo_name(), baseline_name(), False, False)
    recreate_demo()
    msg = ("Demo copy created from the pipeline database." if current is None else
           "The pipeline database was rebuilt since the last session; the demo copy was refreshed from it.")
    return DemoStatus(demo_name(), baseline_name(), True, False, msg)


def connect(database: str | None = None, *, autocommit: bool = False) -> psycopg.Connection:
    """Connection to the demo copy (or to ``database``)."""
    name = database or (baseline_name() if hosted() else demo_name())
    return psycopg.connect(db.server_uri(name), autocommit=autocommit)


def state_token(conn: psycopg.Connection) -> str:
    """Changes whenever the demo data change (new intake run, or the copy was recreated). Used as a
    cache key so screens re-query only when something moved."""
    oid = conn.execute("select oid from pg_database where datname = current_database()").fetchone()[0]
    run = conn.execute("select coalesce(max(run_id), 0) from recon.run_log").fetchone()[0]
    return f"{oid}:{run}"
