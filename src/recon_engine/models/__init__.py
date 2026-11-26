"""Analysis model registry.

Each analysis module exposes ``run(conn, cfg) -> dict`` and is registered by adding ONE line,
its dotted module path, to ``REGISTRY`` below, for example::

    REGISTRY = [
        "recon_engine.models.markov",
    ]

``run_all.py`` imports every registered module in order, calls ``run(conn, cfg)`` with a
psycopg connection to the built database and the configuration from
``recon_engine.datagen.calibration.model_config()``, and writes the returned dict to
``results/<last part of the module name>.json``. The dict should carry, for every number, its
data label (see ``recon_engine.labels.stamp``), the inputs used and the seed.

Models must not read ``data/raw`` directly or anything the generator writes for evaluation;
they work from the database (core, recon, mart) and ``cfg`` only.
"""
REGISTRY: list[str] = [
    "recon_engine.models.markov",
    "recon_engine.models.cashflow",
    "recon_engine.models.credit",
    "recon_engine.models.prioritize",
    "recon_engine.models.reserve",
    "recon_engine.models.drug",
    "recon_engine.models.econ",
]
