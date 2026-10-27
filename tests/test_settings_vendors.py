"""load_settings vendor loading: a missing/blank vendors_file is 'no vendors', not a crash."""
import logging
from pathlib import Path

import yaml

from estamp.settings import load_settings

BASE = Path(__file__).resolve().parent.parent  # repo root, has vendors.csv + state_config.csv


def _write_config(tmp_path: Path, extra: dict) -> Path:
    """Minimal loadable config in tmp_path; state_config_file points at the real seeded CSV."""
    raw = {"state_config_file": str(BASE / "state_config.csv"), **extra}
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return p


def test_missing_vendors_file_loads_with_no_vendors(tmp_path, caplog):
    # Trimmed config with no `vendors_file` key: s.path(...) would resolve to the base dir and
    # open(<dir>) -> IsADirectoryError. Must degrade to vendors == {} + one WARN, not crash.
    cfg = _write_config(tmp_path, {})
    with caplog.at_level(logging.WARNING, logger="estamp"):
        s = load_settings(cfg)
    assert s.vendors == {}
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_real_vendors_csv_loads_the_three_seeded_vendors(tmp_path):
    cfg = _write_config(tmp_path, {"vendors_file": str(BASE / "vendors.csv")})
    s = load_settings(cfg)
    assert set(s.vendors) == {"PAPEREASE", "AGREXP", "SHABDA"}
