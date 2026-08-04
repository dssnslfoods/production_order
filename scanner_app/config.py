"""Config load/save. Stored as config.json next to this file."""
import json
import os
import threading

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(BASE_DIR)
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

_lock = threading.Lock()

DEFAULT_CONFIG = {
    "provider": "claude",  # claude | gemini | openai
    "api_keys": {
        "claude": "",
        "gemini": "",
        "openai": "",
    },
    "models": {
        "claude": "claude-opus-4-8",
        "gemini": "gemini-2.0-flash",
        "openai": "gpt-4o",
    },
    "folders": {
        # relative paths are resolved against scanner_app/
        "inbox": "data/inbox",
        "scanned": "data/scanned",
        "failed": "data/failed",
    },
    "excel_path": os.path.join(PROJECT_DIR, "ใบเบิกวัตถุดิบ.xlsx"),
    "schedule": {
        "enabled": False,
        "interval_minutes": 30,
    },
    "file_types": ["jpg", "jpeg", "png", "pdf", "webp"],
}


def _merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config():
    with _lock:
        if not os.path.exists(CONFIG_PATH):
            _save_unlocked(DEFAULT_CONFIG)
            return json.loads(json.dumps(DEFAULT_CONFIG))
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return _merge(DEFAULT_CONFIG, data)


def save_config(new_config):
    with _lock:
        merged = _merge(DEFAULT_CONFIG, new_config)
        _save_unlocked(merged)
        return merged


def _save_unlocked(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def resolve_path(p):
    """Resolve a possibly-relative folder path against scanner_app/."""
    if os.path.isabs(p):
        return p
    return os.path.join(BASE_DIR, p)


def ensure_folders(cfg):
    paths = {}
    for key, rel in cfg["folders"].items():
        full = resolve_path(rel)
        os.makedirs(full, exist_ok=True)
        paths[key] = full
    return paths


def masked_config(cfg):
    """Return a copy with API keys masked for sending to the browser."""
    out = json.loads(json.dumps(cfg))
    for k, v in out["api_keys"].items():
        out["api_keys"][k] = ("••••" + v[-4:]) if v else ""
    return out
