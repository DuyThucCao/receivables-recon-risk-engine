"""Load ``config/calibration.yaml``, validate it, and apply public overrides.

Each calibration entry is ``{value, source_class, source, retrieved}``. When
``config/public_calibration.yaml`` exists (written by another workstream from public CMS and
FRED files), three groups of assumptions are replaced by public figures:

1. ``denial.first_pass_denial_rate``: each payer's rate is read off the published issuer
   distribution of in-network denial rates at the percentile position given by
   ``denial.denial_rate_percentile`` (linear interpolation between p10, p25, median, p75, p90).
   The result therefore always lies inside the published p10-p90 range.
2. ``drugs.reference_list``: replaced by the CMS Part B payment-limit list (``drugs[]``).
3. ``financial_assumptions.discount_rate``: replaced by SOFR.

Every override is recorded (assumed value, applied value, source) so outputs can say exactly
which numbers were assumptions and which came from public data. If the public file is absent,
or a section in it is marked unavailable, the assumption is kept and labelled as such.
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .. import CONFIG_DIR
from .. import labels

CALIBRATION_PATH = CONFIG_DIR / "calibration.yaml"
PUBLIC_PATH = CONFIG_DIR / "public_calibration.yaml"
REQUIRED_KEYS = ("value", "source_class", "source", "retrieved")


class CalibrationError(ValueError):
    pass


@dataclass
class Calibration:
    """Resolved calibration: plain values, per-entry metadata, and the override log."""

    values: dict[str, dict[str, Any]]
    meta: dict[str, dict[str, dict[str, str]]]
    overrides: list[dict[str, Any]] = field(default_factory=list)
    public_file_used: bool = False
    public_retrieved: str | None = None
    config_sha256: str = ""

    def v(self, section: str, key: str) -> Any:
        return self.values[section][key]

    def entry_meta(self, section: str, key: str) -> dict[str, str]:
        return self.meta[section][key]


def _is_entry(node: Any) -> bool:
    return isinstance(node, dict) and "value" in node and "source_class" in node


def validate(raw: dict) -> list[str]:
    """Return a list of problems (empty if the file follows the entry contract)."""
    problems: list[str] = []
    for section, entries in raw.items():
        if not isinstance(entries, dict):
            problems.append(f"{section}: section must be a mapping")
            continue
        for key, entry in entries.items():
            where = f"{section}.{key}"
            if not isinstance(entry, dict):
                problems.append(f"{where}: entry must be a mapping")
                continue
            for k in REQUIRED_KEYS:
                if k not in entry:
                    problems.append(f"{where}: missing '{k}'")
            sc = entry.get("source_class")
            if sc is not None and sc not in labels.LABELS:
                problems.append(f"{where}: source_class {sc!r} is not one of {labels.LABELS}")
            ret = str(entry.get("retrieved", ""))
            if ret and not (len(ret) == 10 and ret[4] == "-" and ret[7] == "-"):
                problems.append(f"{where}: retrieved {ret!r} is not YYYY-MM-DD")
    return problems


def _interp_percentile(pub: dict, position: float) -> float:
    """Linear interpolation of the published percentiles at ``position`` in [0, 1].

    Positions outside [0.10, 0.90] are clamped to the published p10/p90 so the drawn value
    never leaves the published inter-decile range.
    """
    knots = [(0.10, pub["p10"]), (0.25, pub["p25"]), (0.50, pub["median"]), (0.75, pub["p75"]),
             (0.90, pub["p90"])]
    x = min(max(position, 0.10), 0.90)
    for (x0, y0), (x1, y1) in zip(knots, knots[1:]):
        if x0 <= x <= x1:
            return float(y0 + (y1 - y0) * (x - x0) / (x1 - x0))
    return float(knots[-1][1])


def _section_ok(node: Any, needed: tuple[str, ...]) -> bool:
    return isinstance(node, dict) and node.get("status", "ok") != "unavailable" and all(
        k in node for k in needed)


def load_calibration(path: Path | str = CALIBRATION_PATH,
                     public_path: Path | str | None = PUBLIC_PATH,
                     use_public: bool = True) -> Calibration:
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    raw = yaml.safe_load(text)
    problems = validate(raw)
    if problems:
        raise CalibrationError("calibration.yaml violates the entry contract:\n  " + "\n  ".join(problems))

    values = {s: {k: copy.deepcopy(e["value"]) for k, e in entries.items()} for s, entries in raw.items()}
    meta = {s: {k: {m: str(e[m]) for m in ("source_class", "source", "retrieved")}
                for k, e in entries.items()} for s, entries in raw.items()}
    cal = Calibration(values=values, meta=meta,
                      config_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest())

    pp = Path(public_path) if public_path else None
    if use_public and pp is not None and pp.exists():
        pub_text = pp.read_text(encoding="utf-8")
        pub = yaml.safe_load(pub_text) or {}
        cal.public_retrieved = str(pub.get("retrieved")) if pub.get("retrieved") else None
        _apply_public(cal, pub)
        cal.public_file_used = bool(cal.overrides)
        cal.config_sha256 = hashlib.sha256((text + pub_text).encode("utf-8")).hexdigest()
    return cal


def _apply_public(cal: Calibration, pub: dict) -> None:
    # 1. denial rates
    dr = pub.get("denial_rate_in_network")
    if _section_ok(dr, ("p10", "p25", "median", "p75", "p90")):
        positions = cal.values["denial"]["denial_rate_percentile"]
        assumed = cal.values["denial"]["first_pass_denial_rate"]
        new = {}
        for payer, pos in positions.items():
            new[payer] = round(_interp_percentile(dr, float(pos)), 4)
            cal.overrides.append({
                "parameter": "denial.first_pass_denial_rate", "scope": payer,
                "assumed_value": assumed.get(payer), "applied_value": new[payer],
                "method": f"percentile {pos} of the published issuer distribution (plan year {dr.get('plan_year')}, n={dr.get('n_issuers')})",
                "source_class": dr.get("source_class", labels.OBSERVED_PUBLIC),
                "source": dr.get("source", ""),
            })
        cal.values["denial"]["first_pass_denial_rate"] = new
        cal.meta["denial"]["first_pass_denial_rate"] = {
            "source_class": dr.get("source_class", labels.OBSERVED_PUBLIC),
            "source": "Placed inside the published distribution: " + str(dr.get("source", "")),
            "retrieved": str(pub.get("retrieved", "")),
        }
    # 2. drug reference prices
    dp = pub.get("drug_reference_prices")
    if _section_ok(dp, ("drugs",)) and dp["drugs"]:
        drugs = []
        for d in dp["drugs"]:
            drugs.append({
                "hcpcs": d["hcpcs"], "short_description": d["short_description"],
                "dosage": d["dosage"], "payment_limit_usd": float(d["payment_limit_usd"]),
                "therapeutic_area": d.get("therapeutic_area", "oncology"),
            })
        cal.overrides.append({
            "parameter": "drugs.reference_list", "scope": f"{len(drugs)} HCPCS codes",
            "assumed_value": f"{len(cal.values['drugs']['reference_list'])} assumed drugs",
            "applied_value": f"CMS payment limits, {dp.get('quarter')}",
            "method": "drug list, billing unit and reference price per unit taken from the public file",
            "source_class": dp.get("source_class", labels.PUBLISHED_AGGREGATE),
            "source": dp.get("source", ""),
        })
        cal.values["drugs"]["reference_list"] = drugs
        cal.meta["drugs"]["reference_list"] = {
            "source_class": dp.get("source_class", labels.PUBLISHED_AGGREGATE),
            "source": str(dp.get("source", "")), "retrieved": str(pub.get("retrieved", "")),
        }
    # 3. discount rate from SOFR
    ir = pub.get("interest_rates") or {}
    sofr = ir.get("sofr") if isinstance(ir, dict) else None
    if _section_ok(sofr, ("value_pct",)):
        rate = round(float(sofr["value_pct"]) / 100.0, 6)
        cal.overrides.append({
            "parameter": "financial_assumptions.discount_rate", "scope": "all models",
            "assumed_value": cal.values["financial_assumptions"]["discount_rate"],
            "applied_value": rate,
            "method": f"SOFR as of {sofr.get('as_of')}",
            "source_class": sofr.get("source_class", labels.OBSERVED_PUBLIC),
            "source": sofr.get("source", ""),
        })
        cal.values["financial_assumptions"]["discount_rate"] = rate
        cal.meta["financial_assumptions"]["discount_rate"] = {
            "source_class": sofr.get("source_class", labels.OBSERVED_PUBLIC),
            "source": f"SOFR {sofr.get('value_pct')}% as of {sofr.get('as_of')}: {sofr.get('source', '')}",
            "retrieved": str(pub.get("retrieved", "")),
        }


def model_config(cal: Calibration | None = None) -> dict[str, Any]:
    """The configuration handed to analysis models as ``run(conn, cfg)``.

    It holds only what a real analyst would know: the as-of date, the seed, financial
    assumptions and public inputs, each with its label. It deliberately excludes the
    generator's behavioural parameters (denial coefficients, lag distributions, ...), which
    models are supposed to estimate from the database, not read.
    """
    cal = cal or load_calibration()
    fa = {}
    for key, value in cal.values["financial_assumptions"].items():
        m = cal.meta["financial_assumptions"][key]
        fa[key] = {"value": value, "label": m["source_class"], "source": m["source"]}
    return {
        "as_of_date": cal.v("simulation", "as_of_date"),
        "seed": cal.v("simulation", "seed"),
        "service_start": cal.v("simulation", "service_start"),
        "service_end": cal.v("simulation", "service_end"),
        "financial_assumptions": fa,
        "discount_rate": fa["discount_rate"],
        "public_data_used": sorted({o["parameter"].split(".")[0] for o in cal.overrides} - {"denial"}),
        "calibration_sha256": cal.config_sha256,
    }


def override_record(cal: Calibration) -> dict[str, Any]:
    return {
        "public_file_used": cal.public_file_used,
        "public_retrieved": cal.public_retrieved,
        "overrides": cal.overrides,
        "calibration_sha256": cal.config_sha256,
    }


if __name__ == "__main__":  # pragma: no cover - manual inspection helper
    c = load_calibration()
    print(json.dumps(override_record(c), indent=2, default=str))
