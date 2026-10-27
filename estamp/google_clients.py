"""Google Drive + Sheets wrapper.

Auth
  * oauth            – your own Google account. Best for the Mac pilot: files are owned by you,
                       works in My Drive and Shared Drives. First run opens a browser once.
  * service_account  – for a server later. Must work inside a Shared Drive (service accounts
                       have no My Drive storage).

Thread safety: googleapiclient objects are not thread-safe, so each worker thread builds its own.
Every call retries 429/5xx with backoff (num_retries).
"""
from __future__ import annotations

import io
import threading
from datetime import datetime, timezone
from typing import Any

from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

from .settings import Settings

SCOPES = ["https://www.googleapis.com/auth/drive", "https://www.googleapis.com/auth/spreadsheets"]
FOLDER = "application/vnd.google-apps.folder"
GSHEET = "application/vnd.google-apps.spreadsheet"
PDF = "application/pdf"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
XLS = "application/vnd.ms-excel"
CSV = "text/csv"
R = 5  # retries per call


def load_credentials(settings: Settings):
    mode = settings.get("google.auth", "oauth")
    if mode == "service_account":
        from google.oauth2 import service_account
        return service_account.Credentials.from_service_account_file(
            str(settings.path("google.service_account_file")), scopes=SCOPES)

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    token_path = settings.path("google.oauth_token_file")
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(settings.path("google.oauth_client_file")), SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json())
    return creds


def file_link(file_id: str) -> str:
    return f"https://drive.google.com/file/d/{file_id}/view"


def sheet_link(sid: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{sid}/edit"


def _q(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'")


def _tab(title: str) -> str:
    return "'" + title.replace("'", "''") + "'"


class Google:
    def __init__(self, settings: Settings, credentials=None):
        self.settings = settings
        self.creds = credentials or load_credentials(settings)
        self._local = threading.local()
        self._folder_lock = threading.Lock()
        self._folder_cache: dict[tuple[str, str], str] = {}

    # ---------------------------------------------------------------- per-thread services
    @property
    def drive(self):
        if not hasattr(self._local, "drive"):
            self._local.drive = build("drive", "v3", credentials=self.creds, cache_discovery=False)
        return self._local.drive

    @property
    def sheets(self):
        if not hasattr(self._local, "sheets"):
            self._local.sheets = build("sheets", "v4", credentials=self.creds, cache_discovery=False)
        return self._local.sheets

    # ---------------------------------------------------------------- Drive: read
    def get_file(self, file_id: str) -> dict:
        return self.drive.files().get(fileId=file_id, supportsAllDrives=True,
                                      fields="id,name,mimeType,parents,trashed,modifiedTime,md5Checksum").execute(num_retries=R)

    def list_children(self, folder_id: str, folders: bool | None = None) -> list[dict]:
        q = f"'{folder_id}' in parents and trashed=false"
        if folders is True:
            q += f" and mimeType='{FOLDER}'"
        elif folders is False:
            q += f" and mimeType!='{FOLDER}'"
        out, token = [], None
        while True:
            res = self.drive.files().list(
                q=q, pageSize=1000, pageToken=token, orderBy="modifiedTime",
                supportsAllDrives=True, includeItemsFromAllDrives=True,
                fields="nextPageToken, files(id,name,mimeType,modifiedTime,md5Checksum,parents)").execute(num_retries=R)
            out.extend(res.get("files", []))
            token = res.get("nextPageToken")
            if not token:
                return out

    def child_exists(self, folder_id: str, name: str) -> bool:
        return self.child_id(folder_id, name) is not None

    def child_id(self, folder_id: str, name: str) -> str | None:
        """Id of the (first) child with this exact name, or None – lets callers reuse a file a crashed
        prior run already created instead of uploading a duplicate (#9)."""
        res = self.drive.files().list(
            q=f"'{folder_id}' in parents and name='{_q(name)}' and trashed=false", pageSize=1,
            supportsAllDrives=True, includeItemsFromAllDrives=True, fields="files(id)").execute(num_retries=R)
        files = res.get("files")
        return files[0]["id"] if files else None

    def download(self, file_id: str) -> bytes:
        buf = io.BytesIO()
        req = self.drive.files().get_media(fileId=file_id, supportsAllDrives=True)
        dl = MediaIoBaseDownload(buf, req, chunksize=8 * 1024 * 1024)
        done = False
        while not done:
            _, done = dl.next_chunk(num_retries=R)
        return buf.getvalue()

    # ---------------------------------------------------------------- Drive: write
    def folder_path(self, root_id: str, parts: list[str]) -> str:
        """Return the id of root/parts[0]/parts[1]/…, creating missing folders. Thread-safe."""
        parent = root_id
        for name in parts:
            key = (parent, name)
            if key not in self._folder_cache:
                with self._folder_lock:
                    if key not in self._folder_cache:
                        res = self.drive.files().list(
                            q=f"'{parent}' in parents and name='{_q(name)}' and mimeType='{FOLDER}' and trashed=false",
                            pageSize=1, supportsAllDrives=True, includeItemsFromAllDrives=True,
                            fields="files(id)").execute(num_retries=R)
                        files = res.get("files", [])
                        if files:
                            fid = files[0]["id"]
                        else:
                            fid = self.drive.files().create(
                                body={"name": name, "mimeType": FOLDER, "parents": [parent]},
                                supportsAllDrives=True, fields="id").execute(num_retries=R)["id"]
                        self._folder_cache[key] = fid
            parent = self._folder_cache[key]
        return parent

    def upload_pdf(self, folder_id: str, name: str, data: bytes) -> str:
        media = MediaIoBaseUpload(io.BytesIO(data), mimetype=PDF, resumable=len(data) > 5 * 1024 * 1024)
        return self.drive.files().create(body={"name": name, "parents": [folder_id]}, media_body=media,
                                         supportsAllDrives=True, fields="id").execute(num_retries=R)["id"]

    def copy(self, file_id: str, folder_id: str, name: str) -> str:
        return self.drive.files().copy(fileId=file_id, body={"name": name, "parents": [folder_id]},
                                       supportsAllDrives=True, fields="id").execute(num_retries=R)["id"]

    def move_rename(self, file_id: str, new_parent: str, new_name: str | None = None) -> None:
        """Rename and move in ONE call, so a file is never left half-renamed."""
        current = self.get_file(file_id).get("parents", [])
        body = {"name": new_name} if new_name else {}
        self.drive.files().update(fileId=file_id, body=body, addParents=new_parent,
                                  removeParents=",".join(p for p in current if p != new_parent),
                                  supportsAllDrives=True, fields="id").execute(num_retries=R)

    # ---------------------------------------------------------------- Sheets
    def tab_titles(self, sid: str) -> list[str]:
        meta = self.sheets.spreadsheets().get(spreadsheetId=sid, fields="sheets.properties.title").execute(num_retries=R)
        return [s["properties"]["title"] for s in meta.get("sheets", [])]

    def read_tabs(self, sid: str) -> list[tuple[str, list[list[str]]]]:
        titles = self.tab_titles(sid)
        if not titles:
            return []
        res = self.sheets.spreadsheets().values().batchGet(
            spreadsheetId=sid, ranges=[_tab(t) for t in titles],
            valueRenderOption="FORMATTED_VALUE").execute(num_retries=R)
        return [(t, vr.get("values", [])) for t, vr in zip(titles, res.get("valueRanges", []))]

    def read_range(self, sid: str, a1: str) -> list[list[str]]:
        res = self.sheets.spreadsheets().values().get(spreadsheetId=sid, range=a1,
                                                      valueRenderOption="FORMATTED_VALUE").execute(num_retries=R)
        return res.get("values", [])

    def ensure_tabs(self, sid: str, headers: dict[str, list[str]]) -> None:
        existing = set(self.tab_titles(sid))
        missing = [t for t in headers if t not in existing]
        if missing:
            self.sheets.spreadsheets().batchUpdate(spreadsheetId=sid, body={
                "requests": [{"addSheet": {"properties": {"title": t}}} for t in missing]}).execute(num_retries=R)
            for t in missing:
                self.write_range(sid, f"{_tab(t)}!A1", [headers[t]])

    def overwrite_tab(self, sid: str, title: str, rows: list[list[Any]], value_input: str = "RAW") -> None:
        # value_input="USER_ENTERED" lets a cell beginning with "=" become a live formula (D-2/D-3 report
        # Amount/totals/payable columns). Default RAW keeps every existing caller's text-literal behaviour.
        self.sheets.spreadsheets().values().clear(spreadsheetId=sid, range=_tab(title), body={}).execute(num_retries=R)
        if rows:
            self.write_range(sid, f"{_tab(title)}!A1", rows, value_input)

    def write_range(self, sid: str, a1: str, rows: list[list[Any]], value_input: str = "RAW") -> None:
        self.sheets.spreadsheets().values().update(
            spreadsheetId=sid, range=a1, valueInputOption=value_input,
            body={"values": [[("" if v is None else v) for v in r] for r in rows]}).execute(num_retries=R)

    def write_ranges(self, sid: str, data: list[tuple[str, list[list[Any]]]]) -> None:
        if not data:
            return
        self.sheets.spreadsheets().values().batchUpdate(spreadsheetId=sid, body={
            "valueInputOption": "RAW", "data": [{"range": a1, "values": v} for a1, v in data]}).execute(num_retries=R)

    def append_rows(self, sid: str, title: str, rows: list[list[Any]]) -> None:
        if rows:
            self.sheets.spreadsheets().values().append(
                spreadsheetId=sid, range=f"{_tab(title)}!A1", valueInputOption="RAW",
                insertDataOption="INSERT_ROWS", body={"values": rows}).execute(num_retries=R)

    def create_sheet(self, folder_id: str, name: str, header: list[str]) -> str:
        sid = self.drive.files().create(body={"name": name, "mimeType": GSHEET, "parents": [folder_id]},
                                        supportsAllDrives=True, fields="id").execute(num_retries=R)["id"]
        self.sheets.spreadsheets().batchUpdate(spreadsheetId=sid, body={"requests": [
            {"updateSheetProperties": {"properties": {"sheetId": 0, "title": name, "gridProperties": {"frozenRowCount": 1}},
                                       "fields": "title,gridProperties.frozenRowCount"}}]}).execute(num_retries=R)
        self.write_range(sid, f"{_tab(name)}!A1", [header])
        return sid


def parse_time(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
