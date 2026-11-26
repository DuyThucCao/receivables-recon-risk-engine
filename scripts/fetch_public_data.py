#!/usr/bin/env python
"""Download the small public files this project calibrates against and regenerate
``config/public_calibration.yaml`` from them.

Usage (from the repository root)::

    python -I scripts/fetch_public_data.py
    python -I scripts/fetch_public_data.py --offline
    python -I scripts/fetch_public_data.py --check-latest

What it does
------------
1. Downloads five files (about 2.7 MB in total) into ``data/public/<source_id>/``.
2. Records URL, retrieval time, byte size and SHA-256 of each in ``data/public/manifest.json``.
3. Parses them and rewrites ``config/public_calibration.yaml``.

Failure behaviour
-----------------
* Any download or parse error stops the run with a non-zero exit code and a message naming
  the source. The existing YAML is left untouched, so the rest of the project keeps working
  from the committed file.
* ``--allow-partial`` writes the YAML anyway; a section whose source failed carries
  ``status: unavailable`` and the error text, and its values are omitted (never guessed).
* ``--offline`` does no network access: it re-parses the files already in ``data/public/``
  after checking them against the manifest hashes.

Rules this script follows
-------------------------
* No API keys, no sign-up, no personal information; a generic User-Agent.
* Only cms.gov, download.cms.gov and fred.stlouisfed.org are contacted.
* Nothing larger than 25 MB is downloaded.
* Figures quoted from publications (KFF) cannot be recomputed from a file. They are kept in
  ``PUBLISHED_BENCHMARKS`` below with the sentence they were read from and the date a person
  checked that sentence on the source page. Re-check them by hand before changing them.

Dependencies: requests, openpyxl, pyyaml (all already in the project environment).
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import os
import re
import sys
import zipfile
from pathlib import Path

import openpyxl
import requests
import yaml

REPO = Path(__file__).resolve().parents[1]
PUBLIC_DIR = REPO / "data" / "public"
MANIFEST = PUBLIC_DIR / "manifest.json"
OUT_YAML = REPO / "config" / "public_calibration.yaml"

USER_AGENT = "Mozilla/5.0 (compatible; public-data-fetch/1.0)"
MAX_BYTES = 25 * 1024 * 1024
TIMEOUT = 60
ALLOWED_HOSTS = {"www.cms.gov", "download.cms.gov", "fred.stlouisfed.org"}

SOURCE_CLASSES = {
    "observed_public",
    "published_aggregate",
    "synthetic_record",
    "financial_assumption",
    "model_estimate",
}

# --------------------------------------------------------------------------------------
# Sources. URLs are pinned (not discovered at run time) so that a rerun fetches the same
# release that the documentation describes. To move to a newer release, change the URL
# here, rerun, and update docs/data_sources.md. `--check-latest` tells you when to do so.
# --------------------------------------------------------------------------------------
TIC_LANDING = "https://www.cms.gov/marketplace/resources/data/public-use-files"
ASP_LANDING = "https://www.cms.gov/medicare/payment/part-b-drugs/asp-pricing-files"

SOURCES = {
    "cms_tic_puf_py2027": {
        "url": "https://download.cms.gov/marketplace-puf/2027/transparency-in-coverage-puf.zip",
        "filename": "transparency-in-coverage-puf.zip",
        "publisher": "Centers for Medicare & Medicaid Services (CMS), CCIIO",
        "landing_page": TIC_LANDING,
        "puf_year": 2027,
        "data_year": 2025,  # CMS: "the plan year 2027 PUF contains data from plan year 2025"
    },
    "cms_tic_puf_py2026": {
        "url": "https://download.cms.gov/marketplace-puf/2026/transparency-in-coverage-puf.zip",
        "filename": "transparency-in-coverage-puf.zip",
        "publisher": "Centers for Medicare & Medicaid Services (CMS), CCIIO",
        "landing_page": TIC_LANDING,
        "puf_year": 2026,
        "data_year": 2024,
    },
    "fred_sofr": {
        "url": "https://fred.stlouisfed.org/graph/fredgraph.csv?id=SOFR",
        "filename": "SOFR.csv",
        "publisher": "Federal Reserve Bank of New York, retrieved from FRED (Federal Reserve Bank of St. Louis)",
        "landing_page": "https://fred.stlouisfed.org/series/SOFR",
        "series_id": "SOFR",
    },
    "fred_dgs3mo": {
        "url": "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS3MO",
        "filename": "DGS3MO.csv",
        "publisher": "Board of Governors of the Federal Reserve System (US), retrieved from FRED (Federal Reserve Bank of St. Louis)",
        "landing_page": "https://fred.stlouisfed.org/series/DGS3MO",
        "series_id": "DGS3MO",
    },
    "cms_asp_2026q4": {
        "url": "https://www.cms.gov/files/zip/october-2026-medicare-part-b-payment-limit-files-final.zip",
        "filename": "october-2026-medicare-part-b-payment-limit-files-final.zip",
        "publisher": "Centers for Medicare & Medicaid Services (CMS)",
        "landing_page": ASP_LANDING,
    },
}

# Issuers with fewer in-network claims than this are dropped from the denial-rate distribution.
MIN_IN_NETWORK_CLAIMS = 1000

# Physician-administered drugs to read from the ASP payment limit file. The HCPCS codes are
# the selection; the description, dosage and payment limit all come from the CMS file.
# `therapeutic_area` is this project's own grouping, not a CMS field.
DRUG_SELECTION = [
    ("J9271", "oncology"),
    ("J9299", "oncology"),
    ("J9035", "oncology"),
    ("J9355", "oncology"),
    ("J9312", "oncology"),
    ("J9145", "oncology"),
    ("J9022", "oncology"),
    ("J9306", "oncology"),
    ("J9354", "oncology"),
    ("J9358", "oncology"),
    ("J9228", "oncology"),
    ("J9047", "oncology"),
    ("J1745", "immunology"),
    ("J3380", "immunology"),
    ("J0129", "immunology"),
    ("J3262", "immunology"),
    ("J2357", "immunology"),
    ("J2350", "neurology"),
    ("J2323", "neurology"),
    ("J0178", "ophthalmology"),
    ("J2778", "ophthalmology"),
    ("J2777", "ophthalmology"),
    ("J0897", "bone health"),
    ("J1569", "immune globulin"),
    ("J1303", "hematology"),
]

# Figures read from publications. Each was checked by a person against the source page on
# `verified_on`; `quote` is the sentence it came from. They are not recomputed by this script.
KFF_2024_URL = "https://www.kff.org/private-insurance/claims-denials-and-appeals-in-aca-marketplace-plans-in-2024/"
KFF_2024_CITE = (
    "Long M, Lo J, Pestaina K. Claims Denials and Appeals in ACA Marketplace Plans in 2024. "
    "KFF, published 2026-03-24. " + KFF_2024_URL
)
KFF_2024_SCOPE = (
    "HealthCare.gov individual-market medical plans, 2024 data (CMS Transparency in Coverage "
    "2026 PUF); insurers with more than 1,000 claims; dental and SHOP plans excluded. "
)
KFF_MA_URL = "https://www.kff.org/medicare/medicare-advantage-insurers-made-nearly-53-million-prior-authorization-determinations-in-2024/"
KFF_MA_CITE = (
    "Fuglesten Biniek J, Sroczynski N, Freed M, Neuman T. Medicare Advantage Insurers Made Nearly "
    "53 Million Prior Authorization Determinations in 2024. KFF, published 2026-01-28. " + KFF_MA_URL
)
KFF_MA_SCOPE = (
    "Medicare Advantage prior-authorization requests (decided before the service), 2024. "
    "This is not a claim denial rate and not a commercial-payer figure. "
)
VERIFIED_ON = "2026-11-16"

PUBLISHED_BENCHMARKS = [
    {
        "name": "kff_marketplace_in_network_claim_denial_rate_2024",
        "value": 19,
        "unit": "percent",
        "source_class": "published_aggregate",
        "source": KFF_2024_CITE,
        "note": KFF_2024_SCOPE + "Denials for all reasons, as reported by insurers.",
        "period": "2024",
        "quote": "Insurers of qualified health plans (QHPs) sold on HealthCare.gov denied 19% of in-network claims in 2024 and 37% of out-of-network claims for a combined average of 20% of all claims, all similar to 2023.",
        "verified_on": VERIFIED_ON,
    },
    {
        "name": "kff_marketplace_out_of_network_claim_denial_rate_2024",
        "value": 37,
        "unit": "percent",
        "source_class": "published_aggregate",
        "source": KFF_2024_CITE,
        "note": KFF_2024_SCOPE + "Same sentence as the in-network figure.",
        "period": "2024",
        "quote": "Insurers of qualified health plans (QHPs) sold on HealthCare.gov denied 19% of in-network claims in 2024 and 37% of out-of-network claims for a combined average of 20% of all claims, all similar to 2023.",
        "verified_on": VERIFIED_ON,
    },
    {
        "name": "kff_marketplace_insurer_in_network_denial_rate_min_2024",
        "value": 3,
        "unit": "percent",
        "source_class": "published_aggregate",
        "source": KFF_2024_CITE,
        "note": KFF_2024_SCOPE + "Lowest insurer-level rate in the range KFF reports.",
        "period": "2024",
        "quote": "The in-network denial rate ranged from 3% to 36%, with significant variation by insurer and by state.",
        "verified_on": VERIFIED_ON,
    },
    {
        "name": "kff_marketplace_insurer_in_network_denial_rate_max_2024",
        "value": 36,
        "unit": "percent",
        "source_class": "published_aggregate",
        "source": KFF_2024_CITE,
        "note": KFF_2024_SCOPE + "Highest insurer-level rate in the range KFF reports.",
        "period": "2024",
        "quote": "The in-network denial rate ranged from 3% to 36%, with significant variation by insurer and by state.",
        "verified_on": VERIFIED_ON,
    },
    {
        "name": "kff_marketplace_denied_claims_appealed_share_2024_upper_bound",
        "value": 1,
        "unit": "percent",
        "source_class": "published_aggregate",
        "source": KFF_2024_CITE,
        "note": KFF_2024_SCOPE
        + "The source says 'fewer than 1%'; the value here is that upper bound, not a point estimate. "
        "These are internal appeals filed by enrollees, not provider appeals.",
        "period": "2024",
        "quote": "Consumers rarely appeal denied claims (fewer than 1% of denied claims were appealed), and when they do, insurers usually uphold their original decision (66% of appeals were upheld).",
        "verified_on": VERIFIED_ON,
    },
    {
        "name": "kff_marketplace_internal_appeals_upheld_share_2024",
        "value": 66,
        "unit": "percent",
        "source_class": "published_aggregate",
        "source": KFF_2024_CITE,
        "note": KFF_2024_SCOPE
        + "Share of internal appeals where the insurer upheld its denial (165,863 upheld). "
        "Enrollee appeals, not provider appeals; do not present it as a hospital overturn rate.",
        "period": "2024",
        "quote": "Insurers upheld 165,863 (66%) denials on appeal.",
        "verified_on": VERIFIED_ON,
    },
    {
        "name": "kff_marketplace_denials_administrative_reason_share_2024",
        "value": 25,
        "unit": "percent",
        "source_class": "published_aggregate",
        "source": KFF_2024_CITE,
        "note": KFF_2024_SCOPE
        + "Share of plan-reported in-network denial reasons. Reason data are incomplete and a claim "
        "can carry several reasons.",
        "period": "2024",
        "quote": "Of the limited information available on in-network claims denial reasons, the most common reasons cited by insurers in 2024 included “Other” [reason not listed] at 36%, followed by administrative reasons (25%).",
        "verified_on": VERIFIED_ON,
    },
    {
        "name": "kff_marketplace_denials_prior_authorization_or_referral_share_2024",
        "value": 9,
        "unit": "percent",
        "source_class": "published_aggregate",
        "source": KFF_2024_CITE,
        "note": KFF_2024_SCOPE + "Share of plan-reported in-network denial reasons.",
        "period": "2024",
        "quote": "Nine percent of denials were for lack of prior authorization or referral, and only 5% of denials were for lack of medical necessity.",
        "verified_on": VERIFIED_ON,
    },
    {
        "name": "kff_marketplace_denials_medical_necessity_share_2024",
        "value": 5,
        "unit": "percent",
        "source_class": "published_aggregate",
        "source": KFF_2024_CITE,
        "note": KFF_2024_SCOPE + "Share of plan-reported in-network denial reasons.",
        "period": "2024",
        "quote": "Nine percent of denials were for lack of prior authorization or referral, and only 5% of denials were for lack of medical necessity.",
        "verified_on": VERIFIED_ON,
    },
    {
        "name": "kff_medicare_advantage_prior_auth_denied_share_2024",
        "value": 7.7,
        "unit": "percent",
        "source_class": "published_aggregate",
        "source": KFF_MA_CITE,
        "note": KFF_MA_SCOPE + "Requests denied in full or in part (4.1 million of 52.8 million).",
        "period": "2024",
        "quote": "Medicare Advantage insurers denied 4.1 million (7.7%) prior authorization requests in 2024.",
        "verified_on": VERIFIED_ON,
    },
    {
        "name": "kff_medicare_advantage_prior_auth_denials_appealed_share_2024",
        "value": 11.5,
        "unit": "percent",
        "source_class": "published_aggregate",
        "source": KFF_MA_CITE,
        "note": KFF_MA_SCOPE + "Share of denied requests that were appealed to the insurer.",
        "period": "2024",
        "quote": "Just 11.5% of denied prior authorization requests were appealed to Medicare Advantage insurers in 2024.",
        "verified_on": VERIFIED_ON,
    },
    {
        "name": "kff_medicare_advantage_prior_auth_appeals_overturned_share_2024",
        "value": 80.7,
        "unit": "percent",
        "source_class": "published_aggregate",
        "source": KFF_MA_CITE,
        "note": KFF_MA_SCOPE
        + "Share of appeals partially or fully overturned. Appealed cases are self-selected, so "
        "this is not the overturn probability of an average denial.",
        "period": "2024",
        "quote": "Though a small share of prior authorization denials were appealed to Medicare Advantage insurers, most appeals (80.7%) were partially or fully overturned in 2024.",
        "verified_on": VERIFIED_ON,
    },
]


class SourceError(RuntimeError):
    """A public source could not be downloaded or did not look as expected."""


# --------------------------------------------------------------------------------------
# Download and manifest
# --------------------------------------------------------------------------------------
def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def local_path(source_id: str) -> Path:
    return PUBLIC_DIR / source_id / SOURCES[source_id]["filename"]


def rel(path: Path) -> str:
    return path.relative_to(REPO).as_posix()


def http_get(url: str) -> tuple[bytes, dict]:
    host = re.sub(r"^https://([^/]+)/.*$", r"\1", url)
    if host not in ALLOWED_HOSTS:
        raise SourceError(f"host not on the allow-list: {host}")
    try:
        with requests.get(
            url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT, stream=True
        ) as resp:
            if resp.status_code != 200:
                raise SourceError(f"HTTP {resp.status_code} for {url}")
            declared = resp.headers.get("Content-Length")
            if declared and int(declared) > MAX_BYTES:
                raise SourceError(f"{url} is {declared} bytes, over the {MAX_BYTES} byte limit")
            chunks, total = [], 0
            for chunk in resp.iter_content(chunk_size=1 << 16):
                total += len(chunk)
                if total > MAX_BYTES:
                    raise SourceError(f"{url} exceeded the {MAX_BYTES} byte limit")
                chunks.append(chunk)
            headers = {
                "content_type": resp.headers.get("Content-Type"),
                "last_modified": resp.headers.get("Last-Modified"),
            }
    except requests.RequestException as exc:
        raise SourceError(f"could not reach {url}: {exc}") from exc
    data = b"".join(chunks)
    if not data:
        raise SourceError(f"{url} returned an empty body")
    return data, headers


def download(source_id: str) -> dict:
    spec = SOURCES[source_id]
    data, headers = http_get(spec["url"])
    path = local_path(source_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    now = dt.datetime.now().astimezone()
    return {
        "source_id": source_id,
        "url": spec["url"],
        "landing_page": spec["landing_page"],
        "publisher": spec["publisher"],
        "file": rel(path),
        "retrieved": now.date().isoformat(),
        "retrieved_at": now.isoformat(timespec="seconds"),
        "bytes": len(data),
        "sha256": sha256_bytes(data),
        "http_last_modified": headers["last_modified"],
        "http_content_type": headers["content_type"],
    }


def load_manifest() -> dict:
    if not MANIFEST.exists():
        return {}
    return {e["source_id"]: e for e in json.loads(MANIFEST.read_text())["files"]}


def write_manifest(entries: dict) -> None:
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    # Keep downloaded files out of version control without touching the root .gitignore.
    (PUBLIC_DIR / ".gitignore").write_text("*\n")
    payload = {
        "note": "Written by scripts/fetch_public_data.py. Files in this folder are not committed.",
        "files": [entries[k] for k in sorted(entries)],
    }
    MANIFEST.write_text(json.dumps(payload, indent=2) + "\n")


def verify_offline(source_id: str, manifest: dict) -> dict:
    entry = manifest.get(source_id)
    path = local_path(source_id)
    if entry is None or not path.exists():
        raise SourceError(f"--offline: {rel(path)} or its manifest entry is missing; run without --offline")
    digest = sha256_bytes(path.read_bytes())
    if digest != entry["sha256"]:
        raise SourceError(f"--offline: {rel(path)} does not match the SHA-256 in the manifest")
    return entry


# --------------------------------------------------------------------------------------
# Parsers
# --------------------------------------------------------------------------------------
def zip_member(zip_bytes: bytes, pattern: str) -> tuple[str, bytes]:
    """Return the single archive member whose name matches `pattern`, read in memory."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as exc:
        raise SourceError(f"not a zip archive: {exc}") from exc
    names = [n for n in zf.namelist() if re.search(pattern, n, flags=re.I)]
    if len(names) != 1:
        raise SourceError(f"expected one archive member matching {pattern!r}, found {names}")
    info = zf.getinfo(names[0])
    if info.file_size > 4 * MAX_BYTES:
        raise SourceError(f"archive member {names[0]} is unexpectedly large ({info.file_size} bytes)")
    return names[0], zf.read(names[0])


def to_number(value):
    """Counts arrive as int, float or digit strings; 'N/A', '*', '**' and blanks mean no value."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return float(text)
    return None


def percentile(sorted_values: list[float], q: float) -> float:
    """Linear interpolation between order statistics (the numpy / pandas default)."""
    if not sorted_values:
        raise ValueError("no values")
    pos = (len(sorted_values) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def read_tic_issuers(zip_bytes: bytes) -> tuple[list[dict], str, str]:
    """One record per issuer from the 'Ind QHP' sheet (individual-market medical plans)."""
    member, xlsx = zip_member(zip_bytes, r"\.xlsx$")
    wb = openpyxl.load_workbook(io.BytesIO(xlsx), read_only=True, data_only=True)
    sheets = [s for s in wb.sheetnames if re.search(r"ind\s*qhp", s, flags=re.I)]
    if len(sheets) != 1:
        raise SourceError(f"expected one 'Ind QHP' sheet, found {wb.sheetnames}")
    rows = list(wb[sheets[0]].iter_rows(values_only=True))
    header_idx = next(
        (i for i, r in enumerate(rows[:15]) if r and any(str(c).strip() == "Issuer_ID" for c in r if c)),
        None,
    )
    if header_idx is None:
        raise SourceError(f"no header row with 'Issuer_ID' in sheet {sheets[0]!r}")
    # Column capitalisation differs between PUF years ("Out_of_Network" / "Out_Of_Network").
    cols = {str(c).strip().lower(): i for i, c in enumerate(rows[header_idx]) if c}
    wanted = {
        "issuer_id": "issuer_id",
        "state": "state",
        "issuer_name": "issuer_name",
        "recv_in": "issuer_claims_received_in_network",
        "den_in": "issuer_claims_denied_in_network",
        "resub_in": "issuer_claims_resubmitted_in_network",
        "recv_out": "issuer_claims_received_out_of_network",
        "den_out": "issuer_claims_denied_out_of_network",
        "appeals_filed": "issuer_internal_appeals_claims_filed",
        "appeals_overturned": "issuer_number_internal_appeals_claims_overturned",
    }
    # The two appeal columns were named without "_Claims" before the PY2027 PUF.
    alternates = {
        "appeals_filed": ["issuer_internal_appeals_filed"],
        "appeals_overturned": ["issuer_number_internal_appeals_overturned"],
    }
    required = {"issuer_id", "recv_in", "den_in", "recv_out", "den_out"}
    index = {}
    for key, name in wanted.items():
        found = next((n for n in [name, *alternates.get(key, [])] if n in cols), None)
        if found is None and key in required:
            raise SourceError(f"column {name!r} not found in sheet {sheets[0]!r}")
        index[key] = cols.get(found) if found else None

    issuers: dict[str, dict] = {}
    for row in rows[header_idx + 1 :]:
        if not row or row[index["issuer_id"]] is None:
            continue
        issuer_id = str(row[index["issuer_id"]]).strip()
        record = {k: (row[i] if i is not None else None) for k, i in index.items()}
        record["issuer_id"] = issuer_id
        previous = issuers.get(issuer_id)
        if previous is None:
            issuers[issuer_id] = record
        elif any(
            to_number(previous[k]) != to_number(record[k]) for k in ("recv_in", "den_in")
        ):
            # Issuer-level fields are repeated on every plan row; they must agree.
            raise SourceError(f"issuer {issuer_id} has conflicting issuer-level claim counts")
    if not issuers:
        raise SourceError(f"no issuer rows in sheet {sheets[0]!r}")
    return list(issuers.values()), member, sheets[0]


def tic_denial_stats(issuers: list[dict]) -> dict:
    reported, not_reported, small = [], 0, 0
    for rec in issuers:
        recv, den = to_number(rec["recv_in"]), to_number(rec["den_in"])
        if recv is None or den is None:
            not_reported += 1
        elif recv < MIN_IN_NETWORK_CLAIMS:
            small += 1
        else:
            if den > recv:
                raise SourceError(f"issuer {rec['issuer_id']}: more claims denied than received")
            reported.append(dict(rec, recv=recv, den=den, rate=den / recv))
    if len(reported) < 30:
        raise SourceError(f"only {len(reported)} issuers passed the inclusion rule; the file layout may have changed")
    rates = sorted(r["rate"] for r in reported)
    recv_total = sum(r["recv"] for r in reported)
    den_total = sum(r["den"] for r in reported)
    return {
        "included": reported,
        "n_issuers": len(reported),
        "n_issuers_in_sheet": len(issuers),
        "n_dropped_not_reported": not_reported,
        "n_dropped_small": small,
        "p10": round(percentile(rates, 0.10), 4),
        "p25": round(percentile(rates, 0.25), 4),
        "median": round(percentile(rates, 0.50), 4),
        "p75": round(percentile(rates, 0.75), 4),
        "p90": round(percentile(rates, 0.90), 4),
        "claims_weighted_mean": round(den_total / recv_total, 4),
        "unweighted_mean": round(sum(rates) / len(rates), 4),
        "min": round(rates[0], 4),
        "max": round(rates[-1], 4),
        "claims_received_total": int(recv_total),
        "claims_denied_total": int(den_total),
    }


def tic_extra_benchmarks(included: list[dict], source: str, data_year: int) -> list[dict]:
    """Ratios computed from the same issuers that enter the denial-rate distribution."""
    scope = (
        f"Computed by this project from CMS issuer-level counts for {data_year} "
        f"(HealthCare.gov individual-market medical issuers with at least "
        f"{MIN_IN_NETWORK_CLAIMS:,} in-network claims). "
    )
    out = []

    pairs = [(to_number(r["recv_out"]), to_number(r["den_out"])) for r in included]
    pairs = [(a, b) for a, b in pairs if a is not None and b is not None]
    recv_out, den_out = sum(a for a, _ in pairs), sum(b for _, b in pairs)
    if recv_out > 0:
        out.append(
            {
                "name": f"cms_tic_out_of_network_claim_denial_rate_{data_year}",
                "value": round(100 * den_out / recv_out, 2),
                "unit": "percent",
                "source_class": "observed_public",
                "source": source,
                "note": scope
                + f"Sum of out-of-network claims denied / received over {len(pairs)} issuers "
                f"({int(den_out):,} / {int(recv_out):,}). Denials for all reasons.",
                "period": str(data_year),
            }
        )

    resub = [(r["recv"], to_number(r["resub_in"])) for r in included]
    resub = [(a, b) for a, b in resub if b is not None]
    if resub:
        recv_sum, resub_sum = sum(a for a, _ in resub), sum(b for _, b in resub)
        out.append(
            {
                "name": f"cms_tic_in_network_claims_resubmitted_share_{data_year}",
                "value": round(100 * resub_sum / recv_sum, 2),
                "unit": "percent",
                "source_class": "observed_public",
                "source": source,
                "note": scope
                + f"In-network claims resubmitted one or more times / in-network claims received over "
                f"{len(resub)} issuers with a reported value ({int(resub_sum):,} / {int(recv_sum):,}).",
                "period": str(data_year),
            }
        )

    appeals = [
        (to_number(r["appeals_filed"]), to_number(r["appeals_overturned"]), r["den"], to_number(r["den_out"]))
        for r in included
    ]
    both = [(f, o) for f, o, _, _ in appeals if f is not None and o is not None]
    filed_sum, over_sum = sum(f for f, _ in both), sum(o for _, o in both)
    if filed_sum > 0:
        out.append(
            {
                "name": f"cms_tic_internal_appeals_overturned_share_{data_year}",
                "value": round(100 * over_sum / filed_sum, 2),
                "unit": "percent",
                "source_class": "observed_public",
                "source": source,
                "note": scope
                + f"Internal appeals overturned / filed over the {len(both)} issuers where neither count "
                f"is suppressed ({int(over_sum):,} / {int(filed_sum):,}). CMS suppresses small cells, so "
                "issuers with few appeals are left out. These are appeals by the insured person, not "
                "provider appeals, and appealed claims are self-selected.",
                "period": str(data_year),
            }
        )
    filed_any = [(f, d + (do or 0.0)) for f, _, d, do in appeals if f is not None]
    if filed_any:
        f_sum, d_sum = sum(f for f, _ in filed_any), sum(d for _, d in filed_any)
        out.append(
            {
                "name": f"cms_tic_internal_appeals_filed_per_denied_claim_{data_year}",
                "value": round(100 * f_sum / d_sum, 3),
                "unit": "percent",
                "source_class": "observed_public",
                "source": source,
                "note": scope
                + f"Internal appeals filed / claims denied (in-network plus out-of-network) over "
                f"{len(filed_any)} issuers with a reported appeal count ({int(f_sum):,} / {int(d_sum):,}). "
                "Issuers whose appeal count is suppressed are left out of both sides of the ratio. "
                "Appeals by the insured person, not provider appeals.",
                "period": str(data_year),
            }
        )
    return out


def read_fred_series(csv_bytes: bytes, series_id: str) -> dict:
    reader = csv.reader(io.StringIO(csv_bytes.decode("utf-8-sig")))
    header = next(reader, None)
    if not header or len(header) < 2 or header[1].strip() != series_id:
        raise SourceError(f"unexpected FRED CSV header for {series_id}: {header}")
    observations = []
    for row in reader:
        if len(row) < 2:
            continue
        date, value = row[0].strip(), row[1].strip()
        if value in ("", "."):  # market holiday
            continue
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            raise SourceError(f"unexpected date {date!r} in {series_id}")
        observations.append((date, float(value)))
    if not observations:
        raise SourceError(f"no observations in {series_id}")
    return {
        "as_of": observations[-1][0],
        "value_pct": observations[-1][1],
        "first_observation": observations[0][0],
        "n_observations": len(observations),
    }


QUARTER_START = {"January": 1, "April": 2, "July": 3, "October": 4}


def read_asp(zip_bytes: bytes) -> dict:
    member, raw = zip_member(zip_bytes, r"payment limit file.*\.csv$")
    text = raw.decode("cp1252")  # the CMS CSV is Windows-1252, not UTF-8
    rows = list(csv.reader(io.StringIO(text)))
    header_idx = next((i for i, r in enumerate(rows) if r and r[0].strip() == "HCPCS Code"), None)
    if header_idx is None:
        raise SourceError("no 'HCPCS Code' header row in the ASP payment limit file")
    header = [h.strip() for h in rows[header_idx]]
    try:
        col = {k: header.index(k) for k in (
            "HCPCS Code", "Short Description", "HCPCS Code Dosage", "Payment Limit",
            "Co-insurance Percentage", "Notes")}
    except ValueError as exc:
        raise SourceError(f"ASP file header changed: {header}") from exc

    preamble = " ".join(c for r in rows[:header_idx] for c in r if c)
    eff = re.search(r"Effective (\w+) (\d{1,2}), (\d{4}) through (\w+) (\d{1,2}), (\d{4})", preamble)
    if not eff or eff.group(1) not in QUARTER_START:
        raise SourceError("could not read the effective period from the ASP file preamble")
    basis = re.search(r"based on (\dQ\d\d) ASP data", preamble)

    by_code = {r[0].strip(): r for r in rows[header_idx + 1 :] if r and r[0].strip()}
    drugs, missing = [], []
    for code, area in DRUG_SELECTION:
        row = by_code.get(code)
        limit = to_number(row[col["Payment Limit"]]) if row else None
        if row is None or limit is None:
            missing.append(code)
            continue
        entry = {
            "hcpcs": code,
            "short_description": row[col["Short Description"]].strip(),
            "dosage": row[col["HCPCS Code Dosage"]].strip(),
            "payment_limit_usd": round(limit, 3),
            "coinsurance_pct": to_number(row[col["Co-insurance Percentage"]]),
            "therapeutic_area": area,
        }
        note = row[col["Notes"]].strip()
        if note:
            entry["cms_note"] = note
        drugs.append(entry)
    if not drugs:
        raise SourceError("none of the selected HCPCS codes were found in the ASP file")
    return {
        "quarter": f"{eff.group(3)}Q{QUARTER_START[eff.group(1)]}",
        "effective_period": eff.group(0).replace("Effective ", ""),
        "asp_data_basis": basis.group(1) if basis else None,
        "member": member,
        "n_codes_in_file": len(by_code),
        "drugs": drugs,
        "missing_codes": missing,
    }


# --------------------------------------------------------------------------------------
# YAML sections
# --------------------------------------------------------------------------------------
def tic_source_text(entry: dict, spec: dict) -> str:
    return (
        f"Centers for Medicare & Medicaid Services. {spec['puf_year']} Transparency in Coverage "
        f"Public Use File (data for plan year {spec['data_year']}). {entry['url']} "
        f"(listed on {spec['landing_page']}), retrieved {entry['retrieved']}."
    )


def build_denial_section(entries: dict) -> tuple[dict, list[dict]]:
    spec = SOURCES["cms_tic_puf_py2027"]
    entry = entries["cms_tic_puf_py2027"]
    issuers, member, sheet = read_tic_issuers(local_path("cms_tic_puf_py2027").read_bytes())
    stats = tic_denial_stats(issuers)
    source = tic_source_text(entry, spec)
    section = {
        "source_class": "observed_public",
        "plan_year": spec["data_year"],
        "puf_year": spec["puf_year"],
        "unit": "fraction of in-network claims received that were denied (0.17 = 17%)",
        "n_issuers": stats["n_issuers"],
        "p10": stats["p10"],
        "p25": stats["p25"],
        "median": stats["median"],
        "p75": stats["p75"],
        "p90": stats["p90"],
        "claims_weighted_mean": stats["claims_weighted_mean"],
        "unweighted_mean": stats["unweighted_mean"],
        "min": stats["min"],
        "max": stats["max"],
        "claims_received_total": stats["claims_received_total"],
        "claims_denied_total": stats["claims_denied_total"],
        "n_issuers_in_sheet": stats["n_issuers_in_sheet"],
        "n_dropped_not_reported": stats["n_dropped_not_reported"],
        "n_dropped_small": stats["n_dropped_small"],
        "inclusion_rule": (
            f"Sheet '{sheet}' only (individual-market medical plans; dental and SHOP sheets not used). "
            "One record per Issuer_ID (a HIOS issuer ID is state-specific, so a national carrier counts "
            "once per state). Dropped: issuers whose in-network claims received or denied is not a "
            f"number ('N/A' = new to the Exchange, '*' not available, '**' suppressed), and issuers with "
            f"fewer than {MIN_IN_NETWORK_CLAIMS:,} in-network claims received. "
            "Rate = Issuer_Claims_Denied_In_Network / Issuer_Claims_Received_In_Network."
        ),
        "percentile_method": "linear interpolation between order statistics, each issuer weighted equally",
        "scope": (
            "Issuers of qualified health plans on HealthCare.gov (Federally-facilitated Exchanges and "
            "State-based Exchanges on the Federal Platform). In-network claims with dates of service in "
            f"{spec['data_year']}, denials for all reasons, self-reported by issuers. Claims include "
            "hospital, physician and pharmacy claims."
        ),
        "caveat": (
            "A calibration range for simulated payers, not a hospital denial rate. It does not cover "
            "employer plans, Medicare, Medicaid or state-run exchanges, does not separate hospital "
            "claims, and counts a claim as denied even if it was later paid on resubmission or appeal. "
            "Use the percentiles; the minimum and maximum are single self-reported issuers."
        ),
        "source": source,
        "source_url": entry["url"],
        "source_file": f"{entry['file']} -> {member}",
        "source_sha256": entry["sha256"],
    }
    extra = tic_extra_benchmarks(stats["included"], source, spec["data_year"])

    # Same code on the previous PUF, to compare with KFF's published analysis of that file.
    prior_spec, prior_entry = SOURCES["cms_tic_puf_py2026"], entries.get("cms_tic_puf_py2026")
    if prior_entry is not None:
        prior_issuers, _, _ = read_tic_issuers(local_path("cms_tic_puf_py2026").read_bytes())
        prior = tic_denial_stats(prior_issuers)
        section["method_check_prior_year"] = {
            "purpose": (
                "The same code run on the previous PUF, for comparison with KFF's published analysis "
                "of that file (19% of in-network claims denied in 2024; insurer range 3% to 36%)."
            ),
            "plan_year": prior_spec["data_year"],
            "puf_year": prior_spec["puf_year"],
            "n_issuers": prior["n_issuers"],
            "claims_weighted_mean": prior["claims_weighted_mean"],
            "median": prior["median"],
            "min": prior["min"],
            "max": prior["max"],
            "claims_received_total": prior["claims_received_total"],
            "source": tic_source_text(prior_entry, prior_spec),
            "source_sha256": prior_entry["sha256"],
        }
    return section, extra


def build_rate_section(source_id: str, entries: dict, description: str, licence: str) -> dict:
    spec, entry = SOURCES[source_id], entries[source_id]
    series = read_fred_series(local_path(source_id).read_bytes(), spec["series_id"])
    return {
        "as_of": series["as_of"],
        "value_pct": series["value_pct"],
        "series_file": entry["file"],
        "source_class": "observed_public",
        "source": (
            f"{spec['publisher']}. {description} [{spec['series_id']}]. "
            f"{spec['landing_page']} (CSV: {entry['url']}), retrieved {entry['retrieved']}."
        ),
        "series_id": spec["series_id"],
        "description": description,
        "unit": "percent per annum, daily, not seasonally adjusted",
        "first_observation": series["first_observation"],
        "n_observations": series["n_observations"],
        "licence": licence,
        "source_sha256": entry["sha256"],
    }


def build_drug_section(entries: dict) -> dict:
    entry = entries["cms_asp_2026q4"]
    asp = read_asp(local_path("cms_asp_2026q4").read_bytes())
    section = {
        "quarter": asp["quarter"],
        "source_class": "published_aggregate",
        "source": (
            "Centers for Medicare & Medicaid Services. Medicare Part B Drug Payment Limit File, "
            f"effective {asp['effective_period']}. {entry['url']} "
            f"(listed on {SOURCES['cms_asp_2026q4']['landing_page']}), retrieved {entry['retrieved']}."
        ),
        "effective_period": asp["effective_period"],
        "asp_data_basis": asp["asp_data_basis"],
        "unit": "US dollars per HCPCS billing unit (the 'dosage' column), Medicare Part B payment limit",
        "caveat": (
            "A Medicare reimbursement reference. It is not what a hospital pays its wholesaler "
            "(acquisition cost) and not what a commercial payer reimburses. One administration "
            "usually bills many units, so a low per-unit figure can still be an expensive drug."
        ),
        "selection_rule": (
            "25 physician-administered drugs chosen by this project by HCPCS Level II J-code "
            "(oncology, immunology, neurology, ophthalmology and similar). The choice is editorial; "
            "'therapeutic_area' is this project's label. Every other field is copied from the CMS file."
        ),
        "source_url": entry["url"],
        "source_file": f"{entry['file']} -> {asp['member']}",
        "source_sha256": entry["sha256"],
        "n_codes_in_file": asp["n_codes_in_file"],
        "drugs": asp["drugs"],
    }
    if asp["missing_codes"]:
        section["missing_codes"] = asp["missing_codes"]
        section["missing_codes_note"] = "Selected codes absent from this quarter's file; omitted, not estimated."
    return section


def unavailable(source_class: str, source: str, error: Exception) -> dict:
    return {"status": "unavailable", "error": str(error), "source_class": source_class, "source": source}


def validate(doc: dict) -> None:
    """Fail loudly if a source_class outside the five project labels slipped in."""

    def walk(node):
        if isinstance(node, dict):
            if "source_class" in node and node["source_class"] not in SOURCE_CLASSES:
                raise SourceError(f"invalid source_class: {node['source_class']!r}")
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(doc)
    for key in ("retrieved", "denial_rate_in_network", "interest_rates", "drug_reference_prices", "benchmarks"):
        if key not in doc:
            raise SourceError(f"missing top-level key: {key}")


YAML_HEADER = """\
# Public calibration values. GENERATED by scripts/fetch_public_data.py - do not edit by hand.
# Every number is either parsed from a public file (see data/public/manifest.json for URL,
# retrieval time, size and SHA-256) or quoted from a publication with the sentence it came from.
# What each number can and cannot support is written up in docs/data_sources.md.
# Dates are strings (YYYY-MM-DD). Denial rates are fractions; benchmark values are in `unit`.
"""


def write_yaml(doc: dict) -> None:
    OUT_YAML.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=100, default_flow_style=False)
    tmp = OUT_YAML.with_suffix(".yaml.tmp")
    tmp.write_text(YAML_HEADER + body, encoding="utf-8")
    os.replace(tmp, OUT_YAML)


# --------------------------------------------------------------------------------------
# Optional: warn when CMS has published something newer than the pinned releases
# --------------------------------------------------------------------------------------
def check_latest() -> int:
    months = {"january": 1, "april": 4, "july": 7, "october": 10}
    status = 0
    try:
        page = http_get(TIC_LANDING)[0].decode("utf-8", "replace")
        years = {int(y) for y in re.findall(r"marketplace-puf/(\d{4})/transparency-in-coverage-puf\.zip", page)}
        pinned = SOURCES["cms_tic_puf_py2027"]["puf_year"]
        print(f"Transparency in Coverage PUF: newest listed {max(years)}, pinned {pinned}")
        if max(years) > pinned:
            status = 2
        page = http_get(ASP_LANDING)[0].decode("utf-8", "replace")
        found = re.findall(r"/files/zip/(january|april|july|october)-(\d{4})-[a-z0-9-]*?(?:payment-limit|asp-pricing)", page)
        newest = max((int(y), months[m]) for m, y in found)
        print(f"ASP payment limit file: newest listed {newest[0]}-{newest[1]:02d}, pinned 2026-10")
        if newest > (2026, 10):
            status = 2
    except (SourceError, ValueError) as exc:
        print(f"ERROR: could not check for newer releases: {exc}", file=sys.stderr)
        return 1
    if status:
        print("A newer release exists. Update the pinned URL in SOURCES, rerun, and update docs/data_sources.md.")
    return status


# --------------------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--offline", action="store_true", help="re-parse files already in data/public/")
    parser.add_argument("--allow-partial", action="store_true", help="write the YAML even if a source failed")
    parser.add_argument("--check-latest", action="store_true", help="only report whether CMS has newer releases")
    args = parser.parse_args()

    if args.check_latest:
        return check_latest()

    manifest = load_manifest()
    entries, errors = {}, {}
    for source_id in SOURCES:
        try:
            if args.offline:
                entries[source_id] = verify_offline(source_id, manifest)
            else:
                entries[source_id] = download(source_id)
            e = entries[source_id]
            print(f"ok    {source_id:22s} {e['bytes']:>9,d} bytes  sha256 {e['sha256'][:12]}  {e['url']}")
        except SourceError as exc:
            errors[source_id] = exc
            print(f"FAIL  {source_id:22s} {exc}", file=sys.stderr)
    if not args.offline and entries:
        write_manifest({**manifest, **entries})

    doc: dict = {
        "retrieved": max((e["retrieved"] for e in entries.values()), default=None),
        "generated_by": "scripts/fetch_public_data.py",
    }
    benchmarks: list[dict] = []

    def attempt(label, needs, build, fallback):
        missing = [s for s in needs if s in errors]
        if missing:  # already reported when the download failed
            return fallback(errors[missing[0]])
        try:
            return build()
        except (SourceError, KeyError, ValueError, zipfile.BadZipFile) as exc:
            errors[label] = exc
            print(f"FAIL  {label:22s} {exc}", file=sys.stderr)
            return fallback(exc)

    denial = attempt(
        "denial_rate_in_network",
        ["cms_tic_puf_py2027"],
        lambda: build_denial_section(entries),
        lambda exc: (unavailable("observed_public", SOURCES["cms_tic_puf_py2027"]["url"], exc), []),
    )
    doc["denial_rate_in_network"], extra = denial
    doc["interest_rates"] = {
        "sofr": attempt(
            "interest_rates.sofr",
            ["fred_sofr"],
            lambda: build_rate_section(
                "fred_sofr", entries, "Secured Overnight Financing Rate",
                "FRED copyright status 'Copyrighted: Citation Required' (owner: Federal Reserve Bank of New York)",
            ),
            lambda exc: unavailable("observed_public", SOURCES["fred_sofr"]["landing_page"], exc),
        ),
        "treasury_3m": attempt(
            "interest_rates.treasury_3m",
            ["fred_dgs3mo"],
            lambda: build_rate_section(
                "fred_dgs3mo", entries,
                "Market Yield on U.S. Treasury Securities at 3-Month Constant Maturity, Quoted on an Investment Basis",
                "FRED copyright status 'Public Domain: Citation Requested'",
            ),
            lambda exc: unavailable("observed_public", SOURCES["fred_dgs3mo"]["landing_page"], exc),
        ),
    }
    doc["drug_reference_prices"] = attempt(
        "drug_reference_prices",
        ["cms_asp_2026q4"],
        lambda: build_drug_section(entries),
        lambda exc: {"quarter": None, **unavailable("published_aggregate", SOURCES["cms_asp_2026q4"]["url"], exc), "drugs": []},
    )
    benchmarks.extend(extra)
    benchmarks.extend(PUBLISHED_BENCHMARKS)
    doc["benchmarks"] = benchmarks

    if errors and not args.allow_partial:
        print(
            f"\nERROR: {len(errors)} source(s) failed: {', '.join(errors)}.\n"
            f"{rel(OUT_YAML)} was NOT rewritten; the project keeps using the existing file.\n"
            "Rerun when the source is reachable, or pass --allow-partial to write the sections that worked.",
            file=sys.stderr,
        )
        return 1

    validate(doc)
    write_yaml(doc)
    missing = doc["drug_reference_prices"].get("missing_codes") or []
    print(f"\nwrote {rel(OUT_YAML)}" + (f" (PARTIAL: {', '.join(errors)})" if errors else ""))
    if missing:
        print(f"WARNING: HCPCS codes not in this quarter's ASP file, omitted: {', '.join(missing)}", file=sys.stderr)
    return 3 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
