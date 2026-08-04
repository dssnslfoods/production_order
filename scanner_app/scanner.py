"""Core scan job: read files from the inbox, extract, append to Excel, move files."""
import datetime
import os
import shutil
import threading

import config
import excel_writer
import extractor

_scan_lock = threading.Lock()

# In-memory state surfaced to the web UI.
STATE = {
    "running": False,
    "last_run": None,
    "last_result": None,
    "recent": [],  # most-recent-first list of per-file results
}
_MAX_RECENT = 100


def _now():
    return datetime.datetime.now().replace(microsecond=0)


def _stamp():
    return _now().isoformat(sep=" ")


def _unique_dest(folder, filename):
    dest = os.path.join(folder, filename)
    if not os.path.exists(dest):
        return dest
    base, ext = os.path.splitext(filename)
    i = 1
    while os.path.exists(os.path.join(folder, f"{base}_{i}{ext}")):
        i += 1
    return os.path.join(folder, f"{base}_{i}{ext}")


def list_inbox(cfg=None):
    cfg = cfg or config.load_config()
    paths = config.ensure_folders(cfg)
    exts = {"." + e.lower().lstrip(".") for e in cfg["file_types"]}
    files = []
    for name in sorted(os.listdir(paths["inbox"])):
        full = os.path.join(paths["inbox"], name)
        if os.path.isfile(full) and os.path.splitext(name)[1].lower() in exts:
            files.append(name)
    return files


def scan_once(trigger="manual"):
    """Process every eligible file currently in the inbox. Returns a summary dict."""
    if not _scan_lock.acquire(blocking=False):
        return {"skipped": True, "reason": "มีการสแกนกำลังทำงานอยู่"}
    try:
        STATE["running"] = True
        cfg = config.load_config()
        paths = config.ensure_folders(cfg)
        provider = cfg["provider"]
        api_key = cfg["api_keys"].get(provider, "")
        model = cfg["models"].get(provider, "")
        excel_path = cfg["excel_path"]

        result = {
            "trigger": trigger,
            "started_at": _stamp(),
            "provider": provider,
            "processed": 0, "succeeded": 0, "failed": 0,
            "files": [],
        }

        if not os.path.exists(excel_path):
            result["error"] = f"ไม่พบไฟล์ Excel: {excel_path}"
            STATE["last_result"] = result
            STATE["last_run"] = result["started_at"]
            return result

        for name in list_inbox(cfg):
            src = os.path.join(paths["inbox"], name)
            stamp = _stamp()
            entry = {"file": name, "at": stamp, "status": "", "detail": ""}
            try:
                data = extractor.extract(src, provider, api_key, model)
                added = excel_writer.append_record(
                    excel_path, data, source_file=name,
                    provider=provider, scanned_at=stamp)
                total = sum(added.values())
                dest = _unique_dest(paths["scanned"], name)
                shutil.move(src, dest)
                entry["status"] = "success"
                entry["detail"] = (f"PO {data.get('production_order_no') or '-'} · "
                                   f"เพิ่ม {total} แถว")
                entry["production_order_no"] = data.get("production_order_no")
                entry["rows_added"] = added
                result["succeeded"] += 1
            except Exception as e:  # noqa: BLE001 — surface any failure per-file
                entry["status"] = "failed"
                entry["detail"] = str(e)
                try:
                    excel_writer.log_failure(excel_path, name, provider, stamp, str(e))
                    dest = _unique_dest(paths["failed"], name)
                    shutil.move(src, dest)
                except Exception as move_err:  # noqa: BLE001
                    entry["detail"] += f" | ย้ายไฟล์ไม่สำเร็จ: {move_err}"
                result["failed"] += 1
            result["processed"] += 1
            result["files"].append(entry)
            STATE["recent"].insert(0, entry)
            del STATE["recent"][_MAX_RECENT:]

        result["finished_at"] = _stamp()
        STATE["last_result"] = result
        STATE["last_run"] = result["finished_at"]
        return result
    finally:
        STATE["running"] = False
        _scan_lock.release()
