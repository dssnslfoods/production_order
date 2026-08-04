"""FastAPI app: config UI, manual 'scan now' button, scheduled auto-scan, status/logs."""
import os
import shutil
from contextlib import asynccontextmanager
from typing import Dict, List, Optional

from fastapi import FastAPI, File, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

import config
import scanner
import scheduler

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_folders(config.load_config())
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(title="ใบเบิกวัตถุดิบ Scanner", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(BASE_DIR, "static", "index.html"), encoding="utf-8") as f:
        return f.read()


@app.get("/api/config")
def get_config():
    cfg = config.load_config()
    return config.masked_config(cfg)


class ConfigIn(BaseModel):
    provider: Optional[str] = None
    api_keys: Optional[Dict] = None
    models: Optional[Dict] = None
    excel_path: Optional[str] = None
    schedule: Optional[Dict] = None
    file_types: Optional[List] = None
    folders: Optional[Dict] = None


@app.post("/api/config")
def set_config(body: ConfigIn):
    current = config.load_config()
    patch = body.model_dump(exclude_none=True)

    # Keep existing API keys when the browser sends a masked/blank value.
    if "api_keys" in patch:
        merged_keys = dict(current["api_keys"])
        for k, v in patch["api_keys"].items():
            if v and not v.startswith("••••"):
                merged_keys[k] = v
        patch["api_keys"] = merged_keys

    new_cfg = config.save_config({**current, **patch})
    config.ensure_folders(new_cfg)
    scheduler.apply_config()
    return config.masked_config(new_cfg)


@app.post("/api/scan")
def scan_now():
    return scanner.scan_once(trigger="manual")


@app.get("/api/status")
def status():
    cfg = config.load_config()
    return {
        "running": scanner.STATE["running"],
        "last_run": scanner.STATE["last_run"],
        "last_result": scanner.STATE["last_result"],
        "recent": scanner.STATE["recent"][:30],
        "schedule": scheduler.status(),
        "provider": cfg["provider"],
        "inbox_files": scanner.list_inbox(cfg),
        "excel_exists": os.path.exists(cfg["excel_path"]),
        "excel_path": cfg["excel_path"],
    }


@app.post("/api/upload")
async def upload(files: List[UploadFile] = File(...)):
    cfg = config.load_config()
    paths = config.ensure_folders(cfg)
    exts = {"." + e.lower().lstrip(".") for e in cfg["file_types"]}
    saved, skipped = [], []
    for f in files:
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in exts:
            skipped.append(f.filename)
            continue
        dest = os.path.join(paths["inbox"], os.path.basename(f.filename))
        with open(dest, "wb") as out:
            shutil.copyfileobj(f.file, out)
        saved.append(f.filename)
    return JSONResponse({"saved": saved, "skipped": skipped})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=False)
