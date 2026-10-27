"""Loads config.yaml, .env secrets and state_config.csv into one Settings object."""
from __future__ import annotations

import csv
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

log = logging.getLogger("estamp")


@dataclass
class StateCfg:
    code: str                 # MH
    name: str                 # MAHARASHTRA
    aliases: list[str]        # other spellings seen in Shriram sheets
    bu: str                   # Shriram business unit (info only)
    amounts: list[int]        # allowed stamp duty amounts for this state, e.g. [200]
    article: str              # info only for now
    daily_qty: int            # projected LANs per day
    buffer_days: float        # low-stock alert when stock < daily_qty * buffer_days
    active: bool              # inactive states are rejected at intake and ingest
    cert_codes: list[str]     # state codes that can appear on the certificate (IN-XX...)
    notes: str = ""
    pages_per_paper: int = 2  # non-e-stamp papers of this state: 2 = front + back, 1 = single page (ADR-0006)

    @property
    def low_stock_threshold(self) -> int:
        return int(round(self.daily_qty * self.buffer_days))


@dataclass
class VendorCfg:
    code: str    # PAPEREASE
    name: str    # Paper Ease (the Upload Inbox subfolder ops drop files into)
    active: bool  # inactive vendors get no auto-created inbox folder


@dataclass
class Settings:
    raw: dict[str, Any]
    base_dir: Path
    states: dict[str, StateCfg] = field(default_factory=dict)
    vendors: dict[str, VendorCfg] = field(default_factory=dict)

    # ---- convenience accessors -------------------------------------------------------
    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def env(self, dotted_env_key: str) -> str:
        """Config holds the NAME of an env var; return its value ('' if unset)."""
        name = self.get(dotted_env_key, "")
        return os.environ.get(name, "") if name else ""

    def path(self, dotted: str) -> Path:
        p = Path(self.get(dotted, ""))
        return p if p.is_absolute() else self.base_dir / p

    @property
    def dry_run(self) -> bool:
        return bool(self.get("dry_run", True))

    @property
    def schema(self) -> str:
        s = self.get("db.schema", "shriram_estamp")
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", s):
            raise ValueError(f"Invalid db.schema '{s}'")
        return s

    def folder(self, key: str) -> str:
        fid = self.get(f"folders.{key}", "")
        if not fid:
            raise ValueError(f"folders.{key} is not set in config.yaml")
        return fid


def _split(v: str) -> list[str]:
    return [x.strip() for x in (v or "").split("|") if x.strip()]


def load_states(path: Path) -> dict[str, StateCfg]:
    states: dict[str, StateCfg] = {}
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            code = row["state_code"].strip().upper()
            if not code:
                continue
            states[code] = StateCfg(
                code=code,
                name=row["state_name"].strip().upper(),
                aliases=[a.upper() for a in _split(row.get("aliases", ""))],
                bu=row.get("bu", "").strip(),
                amounts=[int(a) for a in _split(row.get("stamp_duty", ""))],
                article=row.get("article", "").strip(),
                daily_qty=int(row.get("daily_qty") or 0),
                buffer_days=float(row.get("buffer_days") or 1),
                active=row.get("active", "yes").strip().lower() in ("yes", "y", "true", "1"),
                cert_codes=[c.upper() for c in _split(row.get("cert_codes", ""))] or [code],
                notes=row.get("notes", "").strip(),
                pages_per_paper=int(row.get("pages_per_paper") or 2),
            )
    return states


def load_vendors(path: Path) -> dict[str, VendorCfg]:
    vendors: dict[str, VendorCfg] = {}
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            code = row["vendor_code"].strip().upper()
            if not code:
                continue
            vendors[code] = VendorCfg(
                code=code,
                name=row["vendor_name"].strip(),
                active=row.get("active", "yes").strip().lower() in ("yes", "y", "true", "1"),
            )
    return vendors


def load_settings(config_path: str | os.PathLike = "config.yaml") -> Settings:
    config_path = Path(config_path).resolve()
    base = config_path.parent
    load_dotenv(base / ".env")
    with open(config_path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    s = Settings(raw=raw, base_dir=base)
    s.states = load_states(s.path("state_config_file"))
    # vendors_file is optional: unset/blank, or a path that isn't a real file (e.g. s.path()
    # resolving to the base dir when the key is missing), means "no vendors" – warn, don't crash.
    vpath = s.path("vendors_file") if s.get("vendors_file") else None
    if vpath and vpath.is_file():
        s.vendors = load_vendors(vpath)
    else:
        log.warning("vendors_file not set or not a file (%s) – loading no vendors", vpath)
    return s
