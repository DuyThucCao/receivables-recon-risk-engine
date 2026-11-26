"""Same seed -> byte-identical raw files and truth; different seed -> different files."""
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from recon_engine.datagen import generate  # noqa: E402
from recon_engine.datagen.generate import RAW_COLUMNS  # noqa: E402


def digest(d: Path) -> dict:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(d.iterdir()) if p.is_file()}


def test_same_seed_same_bytes(tmp_path):
    a = generate(tmp_path / "a" / "raw", tmp_path / "a" / "truth", n_claims=800)
    b = generate(tmp_path / "b" / "raw", tmp_path / "b" / "truth", n_claims=800)
    assert a == b
    assert digest(tmp_path / "a" / "raw") == digest(tmp_path / "b" / "raw")
    assert digest(tmp_path / "a" / "truth") == digest(tmp_path / "b" / "truth")
    assert set(RAW_COLUMNS) <= set(digest(tmp_path / "a" / "raw"))


def test_different_seed_different_bytes(tmp_path):
    generate(tmp_path / "a" / "raw", tmp_path / "a" / "truth", n_claims=800, seed=1)
    generate(tmp_path / "b" / "raw", tmp_path / "b" / "truth", n_claims=800, seed=2)
    assert digest(tmp_path / "a" / "raw")["claims_export.csv"] != digest(tmp_path / "b" / "raw")["claims_export.csv"]


def test_raw_headers_match_contract(tmp_path):
    generate(tmp_path / "raw", tmp_path / "truth", n_claims=300)
    for name, cols in RAW_COLUMNS.items():
        header = (tmp_path / "raw" / name).read_text().splitlines()[0].split(",")
        assert header == cols, name
