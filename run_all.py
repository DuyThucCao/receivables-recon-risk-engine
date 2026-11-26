#!/usr/bin/env python
"""Rebuild everything from nothing: generate raw documents, build the database, reconcile,
close every day, evaluate against ground truth, run registered models, write results/.

    python run_all.py
    python run_all.py --skip-generate     # reuse data/raw
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from recon_engine.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(["all"] + sys.argv[1:]))
