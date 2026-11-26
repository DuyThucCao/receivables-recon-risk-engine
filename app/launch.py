#!/usr/bin/env python
"""One-command launcher for the app, fully offline.

    python app/launch.py            # http://localhost:8501
    python app/launch.py --port 8600

Turns off Streamlit's usage statistics (no network calls), binds to localhost only, and runs
app/streamlit_app.py. Extra arguments are passed to ``streamlit run``. The database location
follows RRE_PG_DIR / DATABASE_URL exactly like the pipeline (default ~/.cache/receivables-engine/pg).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    app = Path(__file__).resolve().parent / "streamlit_app.py"
    os.environ.setdefault("STREAMLIT_BROWSER_GATHER_USAGE_STATS", "false")
    os.environ.setdefault("STREAMLIT_SERVER_ADDRESS", "localhost")
    os.environ.setdefault("STREAMLIT_CLIENT_TOOLBAR_MODE", "minimal")
    from streamlit.web import cli as stcli

    sys.argv = ["streamlit", "run", str(app), *sys.argv[1:]]
    return stcli.main()


if __name__ == "__main__":
    sys.exit(main())
