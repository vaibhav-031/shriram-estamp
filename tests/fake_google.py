"""In-memory stand-in for estamp.google_clients.Google – same method names, no network."""
from __future__ import annotations

import hashlib
import io
import itertools
import threading
from datetime import datetime, timedelta, timezone

from pypdf import PdfReader

from estamp.google_clients import FOLDER, GSHEET, PDF

_ids = itertools.count(1)


class FakeGoogle:
    def __init__(self):
        self.files: dict[str, dict] = {}
        self.lock = threading.RLock()
        self.sheets_data: dict[str, dict[str, list[list]]] = {}

    # ---------------------------------------------------------------- helpers for tests
    def add(self, name, mime, parent, data=b"", tabs=None, age_min=60):
        fid = f"f{next(_ids)}"
        self.files[fid] = dict(id=fid, name=name, mimeType=mime, parents=[parent] if parent else [], data=data,
                               trashed=False, md5Checksum=hashlib.md5(data).hexdigest() if data else "",
                               modifiedTime=(datetime.now(timezone.utc) - timedelta(minutes=age_min)).isoformat())
        if tabs is not None:
            self.sheets_data[fid] = tabs
        return fid

    def folder(self, name, parent=None):
        return self.add(name, FOLDER, parent)

    def children(self, folder_id):
        with self.lock:
            snapshot = list(self.files.values())
        return [f for f in snapshot if folder_id in f["parents"] and not f["trashed"]]

    def path_of(self, fid):
        parts, cur = [], self.files[fid]
        while cur["parents"]:
            parts.append(cur["name"])
            cur = self.files[cur["parents"][0]]
        return "/".join(reversed(parts))

    # ---------------------------------------------------------------- Drive API surface
    def get_file(self, fid):
        f = self.files[fid]   # like Drive, a field the item lacks is simply absent (L-2: e.g. modifiedTime)
        return {k: f[k] for k in ("id", "name", "mimeType", "parents", "trashed", "modifiedTime", "md5Checksum") if k in f}

    def list_children(self, folder_id, folders=None):
        out = []
        for f in self.children(folder_id):
            if folders is True and f["mimeType"] != FOLDER:
                continue
            if folders is False and f["mimeType"] == FOLDER:
                continue
            out.append(self.get_file(f["id"]))
        return sorted(out, key=lambda f: f.get("modifiedTime", ""))

    def child_exists(self, folder_id, name):
        return self.child_id(folder_id, name) is not None

    def child_id(self, folder_id, name):
        for f in self.children(folder_id):
            if f["name"] == name:
                return f["id"]
        return None

    def download(self, fid):
        return self.files[fid]["data"]

    def folder_path(self, root, parts):
        with self.lock:
            cur = root
            for p in parts:
                hit = [f for f in self.children(cur) if f["name"] == p and f["mimeType"] == FOLDER]
                cur = hit[0]["id"] if hit else self.add(p, FOLDER, cur)
            return cur

    def upload_pdf(self, folder_id, name, data):
        with self.lock:
            return self.add(name, PDF, folder_id, data, age_min=0)

    def copy(self, fid, folder_id, name):
        with self.lock:
            src = self.files[fid]
            return self.add(name, src["mimeType"], folder_id, src["data"], age_min=0)

    def move_rename(self, fid, new_parent, new_name=None):
        with self.lock:
            f = self.files[fid]
            f["parents"] = [new_parent]
            if new_name:
                f["name"] = new_name

    # ---------------------------------------------------------------- Sheets API surface
    def tab_titles(self, sid):
        return list(self.sheets_data.setdefault(sid, {}).keys())

    def read_tabs(self, sid):
        return [(t, [list(map(str, r)) for r in rows]) for t, rows in self.sheets_data[sid].items()]

    def read_range(self, sid, a1):
        tab = a1.split("!")[0].strip("'")
        return [list(map(str, r)) for r in self.sheets_data.get(sid, {}).get(tab, [])[1:]]

    def ensure_tabs(self, sid, headers):
        d = self.sheets_data.setdefault(sid, {})
        for t, h in headers.items():
            d.setdefault(t, [h])

    def overwrite_tab(self, sid, title, rows, value_input="RAW"):
        self.sheets_data.setdefault(sid, {})[title] = [list(r) for r in rows]

    def write_range(self, sid, a1, rows, value_input="RAW"):
        tab = a1.split("!")[0].strip("'")
        self.sheets_data.setdefault(sid, {}).setdefault(tab, [])
        if a1.endswith("A1"):
            self.sheets_data[sid][tab][:len(rows)] = [list(r) for r in rows]

    def write_ranges(self, sid, data):
        for a1, values in data:
            tab, rng = a1.split("!")
            tab = tab.strip("'")
            col = rng[0]
            row = int("".join(ch for ch in rng.split(":")[0] if ch.isdigit()))
            sheet = self.sheets_data[sid][tab]
            start = ord(col) - ord("A")
            r = sheet[row - 1]
            r.extend([""] * (start + len(values[0]) - len(r)))
            r[start:start + len(values[0])] = values[0]

    def append_rows(self, sid, title, rows):
        self.sheets_data.setdefault(sid, {}).setdefault(title, []).extend([list(r) for r in rows])

    def create_sheet(self, folder_id, name, header):
        """M-1: like the real client, an existing sheet of that name in the folder is reused, never duplicated."""
        with self.lock:
            hit = [f for f in self.children(folder_id) if f["name"] == name and f["mimeType"] == GSHEET]
            if hit:
                return hit[0]["id"]
            sid = self.add(name, GSHEET, folder_id)
            self.sheets_data[sid] = {name: [list(header)]}
        return sid
