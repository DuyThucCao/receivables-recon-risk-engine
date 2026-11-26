#!/usr/bin/env python
"""Build the CFO operations report from the pipeline database and results/*.json.

    python scripts/build_reports.py
    python scripts/build_reports.py --out /tmp/cfo.pdf --png   # also render pages to PNG

The database is the one ``run_all.py`` built (RRE_PG_DIR / DATABASE_URL, as everywhere else).
``--png`` renders each page with pymupdf (optional; ``pip install pymupdf``) next to the PDF for a visual check.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recon_engine.reports import cfo_report  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(cfo_report.OUT_PATH), help="output PDF (default reports/cfo_report.pdf)")
    ap.add_argument("--png", action="store_true", help="also render every page to <out>_pN.png (needs pymupdf)")
    a = ap.parse_args(argv)
    out = cfo_report.build(a.out)
    n = cfo_report.page_count(out)
    print(f"wrote {out} ({n} pages)")
    if a.png:
        import pymupdf

        doc = pymupdf.open(out)
        for i, page in enumerate(doc, start=1):
            png = Path(out).with_name(f"{Path(out).stem}_p{i}.png")
            page.get_pixmap(dpi=110).save(png)
            print(f"rendered {png}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
