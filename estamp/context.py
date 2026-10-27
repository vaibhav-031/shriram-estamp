"""Shared run context: settings, DB, Google client, logging, alerts."""
from __future__ import annotations

import logging
import smtplib
import threading
from dataclasses import dataclass, field
from datetime import date, datetime
from email.message import EmailMessage
from logging.handlers import RotatingFileHandler
from zoneinfo import ZoneInfo

import requests

from .db import DB
from .extract import CertMatcher, StateResolver
from .ocr_split import ConfiguredOcr, ConfiguredSplitter
from .settings import Settings

log = logging.getLogger("estamp")

_LEVELS = ("ERROR", "WARN", "INFO")


def slack_body(lines: list[str]) -> str:
    """Render the plain-text alert lines for the Slack code block: drop the "• " bullet and left-align the
    "ERROR · " / "WARN · " level into a fixed-width label so the message text lines up in one column.
    Lines without a level (summary, stock flags) pass through unchanged."""
    out = []
    for ln in lines:
        s = ln[2:] if ln.startswith("• ") else ln
        for level in _LEVELS:
            if s.startswith(f"{level} · "):
                s = f"{level:<5}  {s[len(level) + 3:]}"
                break
        out.append(s)
    return "\n".join(out)


def setup_logging(settings: Settings) -> None:
    if log.handlers:
        return
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    log.addHandler(sh)
    log_dir = settings.base_dir / "logs"
    log_dir.mkdir(exist_ok=True)
    fh = RotatingFileHandler(log_dir / "estamp.log", maxBytes=20 * 1024 * 1024, backupCount=10)
    fh.setFormatter(fmt)
    log.addHandler(fh)


@dataclass
class Stats:
    uploads: int = 0
    papers_in: int = 0
    papers_review: int = 0
    orders_in: int = 0
    rows_rejected: int = 0
    duplicates: int = 0
    conflicts: int = 0
    files_rejected: int = 0
    allocated: int = 0
    errors: int = 0
    waiting: int = 0
    outputs_written: int = 0
    actions: int = 0

    def as_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class Ctx:
    settings: Settings
    db: DB
    google: object                       # Google, or a fake in tests
    ocr: object = None                   # .read(data, name) -> OcrResult; default from config, a fake in tests
    splitter: object = None              # .split(data, name) -> [PaperPdf]; default from config, a fake in tests
    resolver: StateResolver = None       # type: ignore[assignment]
    matcher: CertMatcher = None          # type: ignore[assignment]
    stats: Stats = field(default_factory=Stats)
    alerts: list[str] = field(default_factory=list)
    preview: list[list] = field(default_factory=list)
    dryPending: list[dict] = field(default_factory=list)   # LANs a dry run WOULD queue (for the matching preview)
    dry_papers: list[dict] = field(default_factory=list)   # papers a dry run WOULD stock (for the matching preview)
    dry: bool = True
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self):
        self.resolver = StateResolver(self.settings.states)
        self.matcher = CertMatcher(self.settings.get("intake.cert_patterns"), self.resolver)
        self.dry = self.settings.dry_run
        if self.ocr is None:
            self.ocr = ConfiguredOcr(self.settings)
        if self.splitter is None:
            self.splitter = ConfiguredSplitter(self.settings)

    # ---------------------------------------------------------------- time
    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.settings.get("timezone", "Asia/Kolkata"))

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def today(self) -> date:
        return self.now().date()

    def now_str(self) -> str:
        return self.now().strftime("%Y-%m-%d %H:%M:%S")

    # ---------------------------------------------------------------- events + alerts
    def event(self, level: str, action: str, details: str, ref: str = "", alert_key: str | None = None) -> None:
        """Log to console/file and the events table. WARN/ERROR also become alerts.

        alert_key: recurring warnings (shortage, low stock) alert at most once per day per key.
        """
        getattr(log, {"ERROR": "error", "WARN": "warning"}.get(level, "info"))(f"{action}: {details}")
        try:
            self.db.event(level, action, details, ref)
        except Exception as e:  # logging must never break the run
            log.error(f"could not write event: {e}")
        if level == "ERROR":
            with self._lock:
                self.stats.errors += 1
        if level in ("ERROR", "WARN"):
            if alert_key and not self.db.alert_once(alert_key, self.today()):
                return
            with self._lock:
                self.alerts.append(f"{level} · {action}: {details}")

    def send_alerts(self, subject: str, lines: list[str]) -> None:
        body = "\n".join(lines)
        hook = self.settings.env("alerts.slack_webhook_env")
        if hook:
            try:
                requests.post(hook, json={"text": f"*{subject}*\n```\n{slack_body(lines)[:3500]}\n```"}, timeout=15)
            except Exception as e:
                log.error(f"Slack alert failed: {e}")
        smtp = self.settings.get("alerts.smtp") or {}
        if smtp.get("host") and smtp.get("to"):
            try:
                msg = EmailMessage()
                msg["Subject"], msg["From"], msg["To"] = subject, smtp.get("from"), ", ".join(smtp["to"])
                msg.set_content(body)
                with smtplib.SMTP(smtp["host"], int(smtp.get("port", 587)), timeout=30) as s:
                    s.starttls()
                    user = self.settings.env("alerts.smtp.user_env")
                    if user:
                        s.login(user, self.settings.env("alerts.smtp.password_env"))
                    s.send_message(msg)
            except Exception as e:
                log.error(f"Email alert failed: {e}")

    def heartbeat(self, ok: bool) -> None:
        url = self.settings.env("alerts.heartbeat_url_env")
        if url:
            try:
                requests.get(url if ok else url.rstrip("/") + "/fail", timeout=10)
            except Exception as e:
                log.error(f"heartbeat ping failed: {e}")
