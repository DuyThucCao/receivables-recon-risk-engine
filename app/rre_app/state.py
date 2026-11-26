"""Streamlit glue: make sure the demo database exists once per server process, and cache each
screen's data until the database changes (a new intake or a reset)."""
from __future__ import annotations

from contextlib import contextmanager

import streamlit as st

from . import demo_db, loaders


@st.cache_resource(show_spinner="Preparing the demo copy of the database ...")
def _startup() -> dict:
    try:
        s = demo_db.ensure_demo()
        return {"ok": True, "status": s}
    except Exception as e:  # shown on every screen; the app cannot work without a database
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def startup() -> dict:
    return _startup()


def require_db() -> bool:
    s = startup()
    if not s["ok"]:
        st.error("**The database is not available.** " + s["error"])
        st.markdown("Build it once with `python run_all.py`, then reload this page. "
                    "The embedded server's data directory is set by `RRE_PG_DIR` "
                    "(default `~/.cache/receivables-engine/pg`).")
        return False
    return True


@contextmanager
def connection():
    c = demo_db.connect()
    try:
        yield c
    finally:
        c.close()


def token() -> str:
    with connection() as c:
        return demo_db.state_token(c)


@st.cache_data(show_spinner=False, max_entries=64)
def _cached(name: str, tok: str):
    with connection() as c:
        return loaders.LOADERS[name](c)


def load(name: str):
    """Screen data for ``name`` (see loaders.LOADERS), re-queried only when the database changed."""
    return _cached(name, token())


def clear() -> None:
    _cached.clear()
