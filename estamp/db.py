"""Postgres ledger. Everything lives in its own schema (default shriram_estamp) – no other tables are touched.

Concurrency model
  * One process at a time: a session-level advisory lock wraps each cycle.
  * Inside the process, worker threads reserve papers with SELECT … FOR UPDATE SKIP LOCKED,
    so two threads can never take the same paper.
"""
from __future__ import annotations

import json
import zlib
from contextlib import contextmanager
from datetime import date
from typing import Any, Iterator

import psycopg
from psycopg import errors
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS {schema};
SET search_path TO {schema};

CREATE TABLE IF NOT EXISTS uploads (
  id            BIGSERIAL PRIMARY KEY,
  drive_file_id TEXT NOT NULL UNIQUE,
  file_name     TEXT,
  md5           TEXT,
  status        TEXT NOT NULL DEFAULT 'PROCESSING',   -- PROCESSING | DONE | DUPLICATE | REVIEW (whole file, E105/E106) | FAILED
  papers_found  INT  NOT NULL DEFAULT 0,
  notes         TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS papers (
  id            BIGSERIAL PRIMARY KEY,
  upload_id     BIGINT REFERENCES uploads(id),
  page_from     INT,
  page_to       INT,
  drive_file_id TEXT,
  file_name     TEXT,
  cert_no       TEXT,
  cert_state    TEXT,
  state         TEXT,
  denomination  INT,
  ocr_amount    INT,
  status        TEXT NOT NULL,    -- AVAILABLE | NEEDS_REVIEW | RESERVED | ASSIGNED | ERROR | REJECTED
  order_id      BIGINT,
  ocr_via       TEXT,
  ocr_text      TEXT,
  notes         TEXT,
  added_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS papers_upload_page_uq ON papers(upload_id, page_from);
CREATE UNIQUE INDEX IF NOT EXISTS papers_cert_live_uq ON papers(cert_no)
  WHERE cert_no IS NOT NULL AND status NOT IN ('NEEDS_REVIEW', 'REJECTED');
CREATE INDEX IF NOT EXISTS papers_pick_idx ON papers(state, denomination, status, added_at, id);

CREATE TABLE IF NOT EXISTS orders (
  id             BIGSERIAL PRIMARY KEY,
  lan            TEXT NOT NULL,
  lan_key        TEXT NOT NULL,
  state          TEXT NOT NULL,
  denomination   INT  NOT NULL,
  source_file    TEXT,
  source_file_id TEXT,
  source_tab     TEXT,
  source_row     INT,
  status         TEXT NOT NULL,   -- PENDING | ALLOCATING | DONE | ERROR | CONFLICT | CANCELLED
  paper_id       BIGINT,
  cert_no        TEXT,
  pdf_link       TEXT,
  notes          TEXT,
  received_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS orders_lan_live_uq ON orders(lan_key)
  WHERE status IN ('PENDING', 'ALLOCATING', 'DONE', 'ERROR');
CREATE INDEX IF NOT EXISTS orders_status_idx ON orders(status, state, denomination, id);
CREATE INDEX IF NOT EXISTS orders_lan_idx ON orders(lan_key);

CREATE TABLE IF NOT EXISTS allocations (
  id             BIGSERIAL PRIMARY KEY,
  order_id       BIGINT NOT NULL REFERENCES orders(id),
  paper_id       BIGINT NOT NULL REFERENCES papers(id),
  lan            TEXT NOT NULL,
  cert_no        TEXT,
  state          TEXT NOT NULL,
  denomination   INT  NOT NULL,
  alloc_date     DATE NOT NULL,
  pdf_name       TEXT,
  pdf_file_id    TEXT,
  pdf_link       TEXT,
  backup_file_id TEXT,
  output_written BOOLEAN NOT NULL DEFAULT false,
  cancelled_at   TIMESTAMPTZ,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE allocations ADD COLUMN IF NOT EXISTS verified_cert TEXT;
ALTER TABLE allocations ADD COLUMN IF NOT EXISTS verified_at TIMESTAMPTZ;
ALTER TABLE allocations ADD COLUMN IF NOT EXISTS verified_via TEXT;
ALTER TABLE papers ADD COLUMN IF NOT EXISTS ocr_raw JSONB;      -- OCR API reading per paper (A-05)
ALTER TABLE papers ADD COLUMN IF NOT EXISTS api_cert_no TEXT;         -- pre-edit API values vs the ops correction (E-07)
ALTER TABLE papers ADD COLUMN IF NOT EXISTS api_denomination INT;
ALTER TABLE papers ADD COLUMN IF NOT EXISTS api_state TEXT;
ALTER TABLE papers ADD COLUMN IF NOT EXISTS ops_edited_by TEXT;
ALTER TABLE papers ADD COLUMN IF NOT EXISTS ops_edited_at TIMESTAMPTZ;
ALTER TABLE papers ADD COLUMN IF NOT EXISTS vendor_code TEXT;   -- D-1: which inbox vendor folder the upload came from
CREATE INDEX IF NOT EXISTS alloc_unwritten_idx ON allocations(alloc_date, state, denomination) WHERE NOT output_written;

CREATE TABLE IF NOT EXISTS output_sheets (
  alloc_date     DATE NOT NULL,
  state          TEXT NOT NULL,
  label          TEXT NOT NULL,
  spreadsheet_id TEXT NOT NULL,
  PRIMARY KEY (alloc_date, state, label)
);

CREATE TABLE IF NOT EXISTS events (
  id      BIGSERIAL PRIMARY KEY,
  ts      TIMESTAMPTZ NOT NULL DEFAULT now(),
  level   TEXT NOT NULL,
  action  TEXT NOT NULL,
  details TEXT,
  ref     TEXT
);
CREATE INDEX IF NOT EXISTS events_ts_idx ON events(ts DESC);

CREATE TABLE IF NOT EXISTS alerts_sent (
  alert_key TEXT NOT NULL,
  day       DATE NOT NULL,
  PRIMARY KEY (alert_key, day)
);

CREATE TABLE IF NOT EXISTS runs (
  id          BIGSERIAL PRIMARY KEY,
  started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ,
  ok          BOOLEAN,
  summary     JSONB
);
"""

LIVE_ORDER_STATUSES = ("PENDING", "ALLOCATING", "DONE", "ERROR")


class DB:
    def __init__(self, url: str, schema: str, max_size: int = 12):
        if not url:
            raise ValueError("Database URL is empty – set ESTAMP_DB_URL in .env")
        self.url = url
        self.schema = schema
        self.pool = ConnectionPool(
            url, min_size=1, max_size=max_size, open=True,
            kwargs={"autocommit": True, "row_factory": dict_row},
            configure=lambda c: c.execute(f"SET search_path TO {schema}"),
        )
        self._lock_conn: psycopg.Connection | None = None

    # ---------------------------------------------------------------- infra
    def init_schema(self) -> None:
        with psycopg.connect(self.url, autocommit=True) as c:
            c.execute(SCHEMA_SQL.format(schema=self.schema))

    def table_counts(self) -> dict[str, int]:
        """Row count per table in this schema, enumerated from information_schema (not hardcoded)."""
        return {t: self.q1(f'SELECT count(*) n FROM "{self.schema}"."{t}"')["n"] for t in self._tables()}

    def reset_all_data(self) -> dict[str, int]:
        """Wipe every table in this schema (TRUNCATE ... RESTART IDENTITY CASCADE) in one transaction.
        Keeps the schema and tables so the service keeps working. Returns the per-table row counts it cleared."""
        before = self.table_counts()
        tables = list(before)
        if tables:
            qualified = ", ".join(f'"{self.schema}"."{t}"' for t in tables)
            with self.conn() as c, c.transaction():
                c.execute(f"TRUNCATE {qualified} RESTART IDENTITY CASCADE")
        return before

    def _tables(self) -> list[str]:
        return [r["table_name"] for r in self.q(
            """SELECT table_name FROM information_schema.tables
               WHERE table_schema=%s AND table_type='BASE TABLE' ORDER BY table_name""", (self.schema,))]

    def close(self) -> None:
        self.release_lock()
        self.pool.close()

    @contextmanager
    def conn(self) -> Iterator[psycopg.Connection]:
        with self.pool.connection() as c:
            yield c

    def q(self, sql: str, params: Any = None) -> list[dict]:
        with self.conn() as c:
            cur = c.execute(sql, params)
            return cur.fetchall() if cur.description else []

    def q1(self, sql: str, params: Any = None) -> dict | None:
        rows = self.q(sql, params)
        return rows[0] if rows else None

    def try_lock(self) -> bool:
        key = zlib.crc32(f"estamp:{self.schema}".encode())
        self._lock_conn = psycopg.connect(self.url, autocommit=True)
        got = self._lock_conn.execute("SELECT pg_try_advisory_lock(%s)", (key,)).fetchone()[0]
        if not got:
            self._lock_conn.close()
            self._lock_conn = None
        return bool(got)

    def release_lock(self) -> None:
        if self._lock_conn is not None:
            try:
                self._lock_conn.close()   # closing the session releases the advisory lock
            finally:
                self._lock_conn = None

    # ---------------------------------------------------------------- events / alerts / runs
    def event(self, level: str, action: str, details: str, ref: str = "") -> None:
        self.q("INSERT INTO events(level, action, details, ref) VALUES (%s,%s,%s,%s)",
               (level, action, details[:20000], ref))

    def alert_once(self, key: str, day: date) -> bool:
        """True the first time a key is seen on a given day."""
        row = self.q1("INSERT INTO alerts_sent(alert_key, day) VALUES (%s,%s) ON CONFLICT DO NOTHING RETURNING alert_key",
                      (key, day))
        return row is not None

    def start_run(self) -> int:
        return self.q1("INSERT INTO runs DEFAULT VALUES RETURNING id")["id"]

    def active_run_age_seconds(self) -> float | None:
        """Seconds since the newest still-running cycle started, or None if no cycle is running. When a new
        cycle can't get the lock, this says how long the holder has been going – a value far past a normal
        cycle means the holder is wedged and blocking all work (#3). A hard crash drops the lock, so if the
        lock is held there really is a live holder and the newest unfinished run is it."""
        row = self.q1("SELECT extract(epoch FROM now() - max(started_at)) AS age FROM runs WHERE finished_at IS NULL")
        return float(row["age"]) if row and row["age"] is not None else None

    def finish_run(self, run_id: int, ok: bool, summary: dict) -> None:
        self.q("UPDATE runs SET finished_at=now(), ok=%s, summary=%s WHERE id=%s",
               (ok, json.dumps(summary, default=str), run_id))

    # ---------------------------------------------------------------- uploads / papers
    def upload_get_or_create(self, file_id: str, name: str, md5: str) -> dict:
        row = self.q1("""INSERT INTO uploads(drive_file_id, file_name, md5) VALUES (%s,%s,%s)
                         ON CONFLICT (drive_file_id) DO UPDATE SET updated_at=now() RETURNING *""",
                      (file_id, name, md5))
        return row

    def upload_same_md5_done(self, md5: str, exclude_id: int) -> dict | None:
        if not md5:
            return None
        return self.q1("SELECT * FROM uploads WHERE md5=%s AND id<>%s AND status='DONE' LIMIT 1", (md5, exclude_id))

    def upload_finish(self, upload_id: int, status: str, papers: int, notes: str = "") -> None:
        self.q("UPDATE uploads SET status=%s, papers_found=%s, notes=%s, updated_at=now() WHERE id=%s",
               (status, papers, notes, upload_id))

    def pages_done(self, upload_id: int) -> set[int]:
        return {r["page_from"] for r in self.q("SELECT page_from FROM papers WHERE upload_id=%s", (upload_id,))}

    def cert_is_live(self, cert: str) -> bool:
        return self.q1("SELECT 1 FROM papers WHERE cert_no=%s AND status NOT IN ('NEEDS_REVIEW','REJECTED') LIMIT 1",
                       (cert,)) is not None

    def insert_paper(self, **f: Any) -> int:
        cols = ", ".join(f)
        ph = ", ".join(["%s"] * len(f))
        vals = tuple(Jsonb(v) if isinstance(v, dict) else v for v in f.values())
        return self.q1(f"INSERT INTO papers({cols}) VALUES ({ph}) RETURNING id", vals)["id"]

    def update_paper(self, paper_id: int, **f: Any) -> None:
        sets = ", ".join(f"{k}=%s" for k in f)
        vals = tuple(Jsonb(v) if isinstance(v, dict) else v for v in f.values())
        self.q(f"UPDATE papers SET {sets}, updated_at=now() WHERE id=%s", (*vals, paper_id))

    # ---------------------------------------------------------------- orders
    def existing_orders(self, lan_keys: list[str]) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        if not lan_keys:
            return out
        for r in self.q("""SELECT id, lan_key, state, status FROM orders
                           WHERE lan_key = ANY(%s) AND status <> 'CANCELLED' ORDER BY id""", (lan_keys,)):
            out.setdefault(r["lan_key"], []).append(r)
        return out

    def insert_orders(self, rows: list[tuple]) -> None:
        """rows: (lan, lan_key, state, denom, source_file, source_file_id, tab, row_no, status, notes)"""
        if not rows:
            return
        with self.conn() as c, c.transaction():
            with c.cursor() as cur:
                cur.executemany("""INSERT INTO orders(lan, lan_key, state, denomination, source_file, source_file_id,
                                   source_tab, source_row, status, notes) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", rows)

    def mark_conflict(self, order_ids: list[int], note: str) -> None:
        self.q("""UPDATE orders SET status='CONFLICT', notes=%s, updated_at=now()
                  WHERE id = ANY(%s) AND status IN ('PENDING','ERROR')""", (note, order_ids))

    def pending_groups(self) -> list[dict]:
        return self.q("""SELECT o.state, o.denomination, o.pending, COALESCE(p.avail, 0) AS avail
                         FROM (SELECT state, denomination, count(*) pending FROM orders WHERE status='PENDING' GROUP BY 1,2) o
                         LEFT JOIN (SELECT state, denomination, count(*) avail FROM papers WHERE status='AVAILABLE' GROUP BY 1,2) p
                           USING (state, denomination) ORDER BY o.state, o.denomination""")

    def claim_orders(self, state: str, denom: int, limit: int) -> list[dict]:
        return self.q("""UPDATE orders SET status='ALLOCATING', updated_at=now()
                         WHERE id IN (SELECT id FROM orders WHERE status='PENDING' AND state=%s AND denomination=%s
                                      ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED)
                         RETURNING *""", (state, denom, limit))

    def reserve_paper(self, order: dict) -> dict | None:
        return self.q1("""UPDATE papers SET status='RESERVED', order_id=%s, updated_at=now()
                          WHERE id = (SELECT id FROM papers WHERE state=%s AND denomination=%s AND status='AVAILABLE'
                                      ORDER BY added_at, id LIMIT 1 FOR UPDATE SKIP LOCKED)
                          RETURNING *""", (order["id"], order["state"], order["denomination"]))

    def release_order(self, order_id: int) -> None:
        self.q("UPDATE orders SET status='PENDING', updated_at=now() WHERE id=%s AND status='ALLOCATING'", (order_id,))

    def finish_allocation(self, order: dict, paper: dict, alloc_date: date, pdf_name: str,
                          pdf_file_id: str, pdf_link: str, backup_file_id: str,
                          verified_cert: str | None = None, verified_via: str | None = None) -> None:
        with self.conn() as c, c.transaction():
            c.execute("""INSERT INTO allocations(order_id, paper_id, lan, cert_no, state, denomination, alloc_date,
                                                 pdf_name, pdf_file_id, pdf_link, backup_file_id,
                                                 verified_cert, verified_at, verified_via)
                         VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,CASE WHEN %s::text IS NULL THEN NULL ELSE now() END,%s)""",
                      (order["id"], paper["id"], order["lan"], paper["cert_no"], order["state"], order["denomination"],
                       alloc_date, pdf_name, pdf_file_id, pdf_link, backup_file_id,
                       verified_cert, verified_cert, verified_via))
            c.execute("""UPDATE papers SET status='ASSIGNED', file_name=%s, notes=NULL, updated_at=now() WHERE id=%s""",
                      (pdf_name, paper["id"]))
            c.execute("""UPDATE orders SET status='DONE', paper_id=%s, cert_no=%s, pdf_link=%s, notes=NULL, updated_at=now()
                         WHERE id=%s""", (paper["id"], paper["cert_no"], pdf_link, order["id"]))

    def fail_allocation(self, order: dict, paper: dict | None, msg: str) -> None:
        with self.conn() as c, c.transaction():
            c.execute("UPDATE orders SET status='ERROR', notes=%s, updated_at=now() WHERE id=%s", (msg, order["id"]))
            if paper:
                c.execute("UPDATE papers SET status='ERROR', notes=%s, updated_at=now() WHERE id=%s",
                          (f"{msg} | LAN {order['lan']}", paper["id"]))

    def recover_stuck(self, minutes: int) -> tuple[int, int]:
        o = self.q("""UPDATE orders SET status='ERROR', updated_at=now(),
                      notes='process stopped mid-allocation – check Output/Backup folders before retrying'
                      WHERE status='ALLOCATING' AND updated_at < now() - make_interval(mins => %s) RETURNING id""", (minutes,))
        p = self.q("""UPDATE papers SET status='ERROR', updated_at=now(),
                      notes='process stopped mid-allocation – check where this file is before approving'
                      WHERE status='RESERVED' AND updated_at < now() - make_interval(mins => %s) RETURNING id""", (minutes,))
        return len(o), len(p)

    # ---------------------------------------------------------------- outputs
    def unwritten_allocations(self) -> list[dict]:
        return self.q("""SELECT * FROM allocations WHERE NOT output_written ORDER BY alloc_date, state, denomination, id""")

    def mark_written(self, ids: list[int]) -> None:
        self.q("UPDATE allocations SET output_written=true WHERE id = ANY(%s)", (ids,))

    def output_sheet(self, d: date, state: str, label: str) -> str | None:
        r = self.q1("SELECT spreadsheet_id FROM output_sheets WHERE alloc_date=%s AND state=%s AND label=%s", (d, state, label))
        return r["spreadsheet_id"] if r else None

    def save_output_sheet(self, d: date, state: str, label: str, sid: str) -> None:
        self.q("INSERT INTO output_sheets VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING", (d, state, label, sid))


UniqueViolation = errors.UniqueViolation
