"""Raw-document generator and defect injector (synthetic data only).

``generate()`` writes the seven raw files of DESIGN.md section 3 to ``data/raw/`` and the
planted ground truth to ``data/truth/``. Pipeline code never reads the latter.
"""
from .generate import generate  # noqa: F401
