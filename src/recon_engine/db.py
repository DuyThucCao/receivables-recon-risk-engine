"""Database connection handling.

Uses ``DATABASE_URL`` when it is set (for example a hosted Postgres). Otherwise it starts, or
reuses, an embedded PostgreSQL 16 server from ``pixeltable-pgserver`` whose data directory lives
outside the repository: ``~/.cache/receivables-engine/pg`` by default, overridable with
``RRE_PG_DIR``. The repository sits in a cloud-synced folder whose long path with spaces breaks
Unix socket paths, which is why the data directory is kept elsewhere (DESIGN.md section 4).

The application database is ``receivables`` unless ``RRE_DB_NAME`` says otherwise.
"""
from __future__ import annotations

import os
from pathlib import Path

import psycopg
from psycopg import sql

DEFAULT_PG_DIR = Path.home() / ".cache" / "receivables-engine" / "pg"
DEFAULT_DB_NAME = "receivables"

_server = None  # keep a reference so the embedded server stays up for this process


def pg_dir() -> Path:
    return Path(os.environ.get("RRE_PG_DIR", str(DEFAULT_PG_DIR))).expanduser()


def db_name() -> str:
    return os.environ.get("RRE_DB_NAME", DEFAULT_DB_NAME)


def _embedded_server():
    global _server
    if _server is None:
        import pixeltable_pgserver as pgserver

        d = pg_dir()
        d.parent.mkdir(parents=True, exist_ok=True)
        _server = pgserver.get_server(d)
    return _server


def server_uri(database: str = "postgres") -> str:
    """Connection URI for ``database`` on the configured server (no existence check)."""
    url = os.environ.get("DATABASE_URL")
    if url:
        if database == "postgres" or database == db_name():
            return url
        # same server, different database: swap the path component
        from urllib.parse import urlsplit, urlunsplit

        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.netloc, "/" + database, parts.query, parts.fragment))
    return _embedded_server().get_uri(database)


def ensure_database(name: str | None = None) -> str:
    """Create database ``name`` if missing (embedded server only) and return its URI."""
    name = name or db_name()
    if os.environ.get("DATABASE_URL"):
        return server_uri(name)
    with psycopg.connect(server_uri("postgres"), autocommit=True) as c:
        exists = c.execute("select 1 from pg_database where datname = %s", (name,)).fetchone()
        if not exists:
            c.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
    return server_uri(name)


def drop_database(name: str) -> None:
    """Drop a database on the embedded server (used by tests for throw-away databases)."""
    if os.environ.get("DATABASE_URL"):
        raise RuntimeError("refusing to drop a database on an external DATABASE_URL server")
    if name == "postgres":
        raise ValueError("refusing to drop the maintenance database")
    with psycopg.connect(server_uri("postgres"), autocommit=True) as c:
        c.execute(sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(name)))


def connect(database: str | None = None, *, autocommit: bool = False) -> psycopg.Connection:
    """Open a connection to the application database (created on first use)."""
    uri = ensure_database(database or db_name())
    return psycopg.connect(uri, autocommit=autocommit)
