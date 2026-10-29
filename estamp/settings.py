"""Loads config.yaml, .env secrets and state_config.csv into one Settings object."""
from __future__ import annotations

import csv
import logging
import os
import re
import shlex
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from .constants import (BACKUP_ROOT, INVENTORY_ROOT, NEEDS_REVIEW, ORDERS_INCOMING, ORDERS_PROCESSED,
                        ORDERS_REJECTED, OUTPUT_ROOT, UPLOAD_ARCHIVE, UPLOAD_INBOX)

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
        if not fid or str(fid).startswith("REPLACE_WITH"):   # shipped placeholder = not set (never send it to Drive)
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


# Folders a cycle touches. reconciliation_root is `report`-only (config.dryrun.yaml deliberately omits it) and
# ocr_temp went away with Drive OCR, so neither is required to run.
RUN_FOLDERS = (UPLOAD_INBOX, UPLOAD_ARCHIVE, NEEDS_REVIEW, INVENTORY_ROOT, ORDERS_INCOMING, ORDERS_PROCESSED,
               ORDERS_REJECTED, OUTPUT_ROOT, BACKUP_ROOT)


def startup_problems(s: Settings) -> list[str]:
    """Everything a cycle needs that config.yaml / .env lack, checked once before any Drive/DB work (L-1) – a
    missing key must stop `run` up front, not fail mid-cycle after intake already moved files. [] = good to go.
    Mirrors the lazy checks in Settings.folder(), ocr_split._ocr_zoop/_split_zoop and DB()."""
    out = []
    for key in RUN_FOLDERS:
        try:
            s.folder(key)
        except ValueError as e:
            out.append(str(e))
    if not s.env("db.url_env"):
        out.append(f"{s.get('db.url_env') or 'db.url_env'} is not set in .env (the Postgres URL)")
    ocr = s.get("zoop_api.ocr") or {}
    if not (s.env("zoop_api.ocr.url_env") or ocr.get("url")) or not ocr.get("config_id"):
        out.append("OCR endpoint not set – set ESTAMP_OCR_URL in .env (or zoop_api.ocr.url) and zoop_api.ocr.config_id")
    for k in ("api_key_env", "org_acc_env"):
        if not s.env(f"zoop_api.ocr.{k}"):
            out.append(f"{ocr.get(k) or 'zoop_api.ocr.' + k} is not set in .env (OCR API)")
    if s.get("intake.split_provider", "local") == "zoop_api" and not s.get("zoop_api.split.url"):
        out.append("zoop_api.split.url is not set (intake.split_provider is zoop_api)")
    sa, cred = _credential_key(s)
    if not s.get(cred) or not s.path(cred).is_file():
        out.append(f"{cred} ({s.get(cred) or 'unset'}) does not exist"
                   + ("" if sa else " – run `python -m estamp login` first"))
    return out


def _credential_key(s: Settings) -> tuple[bool, str]:
    """(service account?, config key of the one Google credential file a run loads)."""
    sa = s.get("google.auth", "oauth") == "service_account"
    return sa, "google.service_account_file" if sa else "google.oauth_token_file"


def secret_file_problems(s: Settings) -> list[str]:
    """.env and the Google credential file a run loads, if group/other can read them (H-2): on a shared host a
    readable token is a full-Drive compromise. Stats modes only – never reads contents. client_secret.json is only
    read by `login`, so it isn't checked here. Skipped on Windows, where st_mode carries no real group/other bits."""
    if os.name == "nt":
        return []
    _, cred = _credential_key(s)
    paths = [s.base_dir / ".env"] + ([s.path(cred)] if s.get(cred) else [])
    out = []
    for p in paths:
        try:
            mode = p.stat().st_mode
        except OSError:
            continue                  # not there (optional, or caught by startup_problems)
        if stat.S_ISREG(mode) and mode & 0o077:
            out.append(f"{p} is readable by other users (mode {mode & 0o777:o}) – run: chmod 600 {shlex.quote(str(p))}")
    return out
