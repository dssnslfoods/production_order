"""Pull new files from a shared Google Drive folder into the scan queue.

The Cloud Run service account mints a Drive-scoped token (by impersonating itself),
so the user only needs to SHARE a Drive folder with the SA email. Pulled files are
moved into a 'scanned' subfolder so they are not picked up again.
"""
import io
import os
import re
import urllib.request

import google.auth
from google.auth import impersonated_credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

import firestore_store as store

_SCOPES = ["https://www.googleapis.com/auth/drive"]
_source, _ = google.auth.default()


def sa_email():
    """Service-account email the user must share their Drive folder with."""
    v = os.environ.get("SA_EMAIL", "")
    if v:
        return v
    try:
        req = urllib.request.Request(
            "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/email",
            headers={"Metadata-Flavor": "Google"})
        return urllib.request.urlopen(req, timeout=2).read().decode()
    except Exception:  # noqa: BLE001
        return ""


def parse_folder_id(s):
    s = (s or "").strip()
    m = re.search(r"/folders/([A-Za-z0-9_-]+)", s)
    if m:
        return m.group(1)
    m = re.search(r"[?&]id=([A-Za-z0-9_-]+)", s)
    if m:
        return m.group(1)
    return s  # already a bare id


def _drive():
    creds = impersonated_credentials.Credentials(
        source_credentials=_source,
        target_principal=sa_email(),
        target_scopes=_SCOPES,
        lifetime=1800,
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _ensure_done_folder(drive, parent_id, name="scanned"):
    q = (f"'{parent_id}' in parents and name='{name}' and "
         "mimeType='application/vnd.google-apps.folder' and trashed=false")
    r = drive.files().list(q=q, fields="files(id)", pageSize=1,
                           supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
    if r.get("files"):
        return r["files"][0]["id"]
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id]}
    return drive.files().create(body=meta, fields="id", supportsAllDrives=True).execute()["id"]


def pull(folder_id, max_files=200):
    """Download new image/PDF files from the folder into the queue; move them to 'scanned'."""
    drive = _drive()
    done_id = _ensure_done_folder(drive, folder_id)
    q = (f"'{folder_id}' in parents and trashed=false and "
         "(mimeType contains 'image/' or mimeType='application/pdf')")
    pulled, names = 0, []
    page = None
    while True:
        r = drive.files().list(
            q=q, fields="nextPageToken, files(id,name,mimeType)", pageSize=100,
            pageToken=page, supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
        for f in r.get("files", []):
            if pulled >= max_files:
                break
            buf = io.BytesIO()
            dl = MediaIoBaseDownload(buf, drive.files().get_media(fileId=f["id"]))
            done = False
            while not done:
                _, done = dl.next_chunk()
            store.add_pending(buf.getvalue(), f["mimeType"], f["name"], user_email="drive-folder")
            drive.files().update(fileId=f["id"], addParents=done_id, removeParents=folder_id,
                                 fields="id", supportsAllDrives=True).execute()
            pulled += 1
            names.append(f["name"])
        page = r.get("nextPageToken")
        if not page or pulled >= max_files:
            break
    return {"pulled": pulled, "files": names}
