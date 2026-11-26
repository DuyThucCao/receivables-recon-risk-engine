"""Calibration contract: every entry labelled; public overrides stay inside published ranges."""
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from recon_engine import labels  # noqa: E402
from recon_engine.datagen.calibration import load_calibration, model_config, validate  # noqa: E402


def test_every_entry_has_value_label_source_date():
    raw = yaml.safe_load((ROOT / "config" / "calibration.yaml").read_text())
    assert validate(raw) == []
    for section, entries in raw.items():
        for key, e in entries.items():
            assert e["source_class"] in labels.LABELS, (section, key)


def test_runs_without_public_file():
    cal = load_calibration(public_path=ROOT / "config" / "does_not_exist.yaml")
    assert not cal.public_file_used and cal.overrides == []
    assert cal.entry_meta("denial", "first_pass_denial_rate")["source_class"] == labels.FINANCIAL_ASSUMPTION


def test_public_overrides_when_present(tmp_path):
    pub = {"retrieved": "2026-11-16",
           "denial_rate_in_network": {"source_class": "observed_public", "plan_year": 2025, "n_issuers": 10,
                                      "p10": 0.05, "p25": 0.10, "median": 0.15, "p75": 0.20, "p90": 0.25, "source": "test"},
           "interest_rates": {"sofr": {"as_of": "2026-11-13", "value_pct": 3.5, "source_class": "observed_public", "source": "test"}},
           "drug_reference_prices": {"quarter": "2026Q4", "source_class": "published_aggregate", "source": "test",
                                     "drugs": [{"hcpcs": "J9999", "short_description": "Test drug", "dosage": "1 MG",
                                                "payment_limit_usd": 10.0, "therapeutic_area": "oncology"}]},
           "benchmarks": []}
    p = tmp_path / "pub.yaml"
    p.write_text(yaml.safe_dump(pub))
    cal = load_calibration(public_path=p)
    rates = cal.v("denial", "first_pass_denial_rate")
    assert all(0.05 <= r <= 0.25 for r in rates.values())
    assert cal.v("financial_assumptions", "discount_rate") == 0.035
    assert [d["hcpcs"] for d in cal.v("drugs", "reference_list")] == ["J9999"]
    params = {o["parameter"] for o in cal.overrides}
    assert params == {"denial.first_pass_denial_rate", "drugs.reference_list", "financial_assumptions.discount_rate"}


def test_unavailable_public_section_is_ignored(tmp_path):
    p = tmp_path / "pub.yaml"
    p.write_text(yaml.safe_dump({"retrieved": "2026-11-16", "denial_rate_in_network": {"status": "unavailable"}}))
    cal = load_calibration(public_path=p)
    assert cal.overrides == []


def test_model_config_hides_generator_parameters():
    cfg = model_config(load_calibration())
    assert "discount_rate" in cfg and cfg["discount_rate"]["label"] in labels.LABELS
    text = repr(cfg)
    for secret in ("first_pass_denial_rate", "overturn_reason_logit", "adjudication_lag_gamma", "mean_shortfall", "reason_mix"):
        assert secret not in text


def test_labels_helpers():
    assert labels.stamp(1.5, labels.MODEL_ESTIMATE)["label"] == "model_estimate"
    import pytest
    with pytest.raises(ValueError):
        labels.check("made_up")
