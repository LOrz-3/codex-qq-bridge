# -*- coding: utf-8 -*-
"""Config loading shared by the engine and every channel adapter."""
import json
import os


DEFAULT_CONFIG = {
    "owner": "",
    "channels": {},
    "codex": {
        "cdp_port": 9229,
        "threads_db": "",
        "codex_dir": "",
        "legacy_codex_dir": "",
        "log_dir": "",
        "page_title": "Codex",
    },
    "paths": {"queue_dir": "", "files_dir": ""},
    "tuning": {"poll_interval_sec": 2.5, "choice_ttl_minutes": 30},
}


def _merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path=None, search_dir=None):
    """Load config.json. Explicit path wins; else config.json next to the
    entry point (search_dir or CWD); else built-in defaults."""
    cfg_path = path
    if not cfg_path:
        base = search_dir or os.getcwd()
        candidate = os.path.join(base, "config.json")
        if os.path.isfile(candidate):
            cfg_path = candidate
    cfg = _merge(DEFAULT_CONFIG, {})
    if cfg_path and os.path.isfile(cfg_path):
        # utf-8-sig tolerates a UTF-8 BOM (PowerShell Set-Content writes one)
        with open(cfg_path, "r", encoding="utf-8-sig") as f:
            cfg = _merge(cfg, json.load(f))
    return cfg, cfg_path


def resolve_paths(cfg, home_base=None):
    """Resolve directory paths from config with sensible defaults."""
    base = cfg.get("codex", {}).get("codex_dir") or home_base or os.path.expanduser("~/.codex")
    queue_dir = cfg.get("paths", {}).get("queue_dir") or os.path.join(base, "feedback")
    files_dir = cfg.get("paths", {}).get("files_dir") or os.path.join(base, "files")
    threads_db = cfg.get("codex", {}).get("threads_db") or os.path.join(base, "state_5.sqlite")
    legacy = cfg.get("codex", {}).get("legacy_codex_dir") or os.path.join(
        os.path.expanduser("~"), ".codex"
    )
    log_dir = cfg.get("codex", {}).get("log_dir") or os.getcwd()
    return {
        "base": base,
        "queue_dir": queue_dir,
        "queue_file": os.path.join(queue_dir, "queue.jsonl"),
        "choices_file": os.path.join(queue_dir, "choices.json"),
        "files_dir": files_dir,
        "threads_db": threads_db,
        "legacy_codex_dir": legacy,
        "log_dir": log_dir,
    }
