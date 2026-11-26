"""Streamlit application for the receivables reconciliation and risk engine.

Everything shown comes from the pipeline: the marts in the embedded PostgreSQL database and the
JSON files in ``results/``. The data are simulated for a fictional hospital ("Meridian Health
System") and six fictional payers.

Modules
-------
paths      repository paths and sys.path setup for ``recon_engine``
demo_db    the demo copy of the database that the reconciliation desk writes to, and its reset
loaders    one data-loading function per screen (plain functions, no Streamlit, testable)
batches    sample remittance / bank / claim inputs in the raw export formats
ui         small presentation helpers (data-label badges, number formats, chart styling)
views      one module per screen, each with ``render()``
"""
from . import paths  # noqa: F401  (puts src/ on sys.path)
