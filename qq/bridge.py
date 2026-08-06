# -*- coding: utf-8 -*-
"""Codex <-> QQ bridge via NapCat OneBot 11 WebSocket + CDP to the desktop app.

Flow (shared-session mode):
  - phone QQ private message -> bridge -> injected into the *current* desktop
    Codex conversation via CDP (port 9229). The desktop window shows it live.
  - a watcher thread polls the conversation text; when a new assistant reply
    ("ChatGPT 说: ...") appears, it is pushed back to the owner's QQ.
  - if CDP is unreachable the message is appended to the feedback queue as a
    fallback so AGENTS.md rules can still pick it up later.

Commands (private chat to the bot QQ, configured as bot_qq):
  #会话            list recent conversations
  #切 <N>          switch to the Nth conversation
  #同步 [N]        pull the last N (default 5) turns of the current conversation
  #日报            summarize today's updated conversations
  #新对话          start a new conversation
  #日常            switch to the daily-chat conversation
  anything else    send to the current conversation

Everything is configured in config.json (copy of config.example.json).
The bridge talks to NapCat over OneBot 11 WebSocket and to the desktop Codex
app over Chrome DevTools Protocol; no cloud account, VPN, or third-party
service is required.
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import cdp  # noqa: E402

FIXED_SIDEBAR = {"工作", "新对话", "已安排", "插件", "置顶", "最近"}
POLL_INTERVAL = 2.5
CHOICE_TTL = 30 * 60  # choices stay valid for 30 minutes


# ---------------------------------------------------------------------------
# configuration loading
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "owner_qq": "",
    "bot_qq": "",
    "napcat": {"ws_url": "ws://127.0.0.1:3001"},
    "codex": {
        "cdp_port": 9229,
        "threads_db": "",
        "codex_dir": "",
        "legacy_codex_dir": "",
        "log_dir": HERE,
    },
    "paths": {"queue_dir": "", "qq_files_dir": ""},
    "tuning": {"poll_interval_sec": 2.5, "choice_ttl_minutes": 30},
}


def _merge(base, override):
    """Recursively merge override dict into base dict."""
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path=None):
    """Load config.json. Explicit --config path wins, then config.json next
    to this file, then built-in defaults."""
    cfg_path = path
    if not cfg_path:
        candidate = os.path.join(HERE, "config.json")
        if os.path.isfile(candidate):
            cfg_path = candidate
    cfg = _merge(DEFAULT_CONFIG, {})
    if cfg_path and os.path.isfile(cfg_path):
        # utf-8-sig tolerates a UTF-8 BOM (PowerShell Set-Content writes one)
        with open(cfg_path, "r", encoding="utf-8-sig") as f:
            user_cfg = json.load(f)
        cfg = _merge(cfg, user_cfg)
    return cfg, cfg_path


def _cfg_paths(cfg):
    """Resolve directory paths from config, falling back to the user's home
    .codex dir so the bridge works with zero path config on a fresh machine."""
    base = cfg["codex"].get("codex_dir") or os.path.expanduser("~/.codex")
    qd = cfg["paths"].get("queue_dir") or os.path.join(base, "feedback")
    qf = cfg["paths"].get("qq_files_dir") or os.path.join(base, "qq-files")
    tdb = cfg["codex"].get("threads_db") or os.path.join(base, "state_5.sqlite")
    legacy = cfg["codex"].get("legacy_codex_dir") or os.path.join(
        os.path.expanduser("~"), ".codex"
    )
    return {
        "base": base,
        "queue_dir": qd,
        "queue_file": os.path.join(qd, "queue.jsonl"),
        "choices_file": os.path.join(qd, "choices.json"),
        "qq_files_dir": qf,
        "threads_db": tdb,
        "legacy_codex_dir": legacy,
    }


CFG = {}
CFG_PATH = None
WS_URL = "ws://127.0.0.1:3001"
OWNER_QQ = ""
BOT_QQ = ""
QUEUE_DIR = ""
QUEUE_FILE = ""
CHOICES_FILE = ""
QQ_FILES_DIR = ""
THREADS_DB = ""
LEGACY_CODEX_DIR = ""
POLL_INTERVAL = 2.5
CHOICE_TTL = 30 * 60
LOG_DIR = HERE


def init_config(cfg_path=None):
    """(Re)load configuration into module globals. Call once at startup and
    again when --config is given on the command line."""
    global CFG, CFG_PATH, WS_URL, OWNER_QQ, BOT_QQ
    global QUEUE_DIR, QUEUE_FILE, CHOICES_FILE, QQ_FILES_DIR
    global THREADS_DB, LEGACY_CODEX_DIR, POLL_INTERVAL, CHOICE_TTL, LOG_DIR
    CFG, CFG_PATH = load_config(cfg_path)
    WS_URL = CFG["napcat"].get("ws_url") or "ws://127.0.0.1:3001"
    OWNER_QQ = str(CFG.get("owner_qq") or "")
    BOT_QQ = str(CFG.get("bot_qq") or "")
    p = _cfg_paths(CFG)
    QUEUE_DIR = p["queue_dir"]
    QUEUE_FILE = p["queue_file"]
    CHOICES_FILE = p["choices_file"]
    QQ_FILES_DIR = p["qq_files_dir"]
    THREADS_DB = p["threads_db"]
    LEGACY_CODEX_DIR = p["legacy_codex_dir"]
    POLL_INTERVAL = float(CFG["tuning"].get("poll_interval_sec") or 2.5)
    CHOICE_TTL = int(CFG["tuning"].get("choice_ttl_minutes") or 30) * 60
    LOG_DIR = CFG["codex"].get("log_dir") or HERE
    cdp.DEBUG_PORT = int(CFG["codex"].get("cdp_port") or 9229)
    # running under pythonw (no console): keep logs in files
    if sys.stdout is None or sys.stderr is None:
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            if sys.stdout is None:
                sys.stdout = open(os.path.join(LOG_DIR, "bridge.out.log"), "a", encoding="utf-8")
            if sys.stderr is None:
                sys.stderr = open(os.path.join(LOG_DIR, "bridge.err.log"), "a", encoding="utf-8")
        except Exception:
            pass


init_config()


# ---------------------------------------------------------------------------
# queue helpers (fallback path)
# ---------------------------------------------------------------------------

def ensure_queue():
    os.makedirs(QUEUE_DIR, exist_ok=True)
    if not os.path.isfile(QUEUE_FILE):
        open(QUEUE_FILE, "a", encoding="utf-8").close()


def append_feedback(qq, text, raw_ts=None):
    ensure_queue()
    entry = {"ts": raw_ts or time.time(), "from": str(qq), "text": text}
    with open(QUEUE_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print("[queue] from %s: %s" % (qq, text[:60]), flush=True)


# ---------------------------------------------------------------------------
# choice protocol: Codex offers [N] options, the bridge turns "1"/"选2"/"B"
# into an explicit "user chose option N: label" before injecting.
# ---------------------------------------------------------------------------

OPTION_RE = re.compile(r"(?m)^\s*(?:[-*•]\s*)?\[\s*(\d+)\s*\]\s*(.+)")
CHOICE_NUM_RE = re.compile(r"^(选|选择|option|opt)?\s*(\d{1,2})\s*(号|项)?$", re.IGNORECASE)
CHOICE_LETTER_RE = re.compile(r"^([A-C])$", re.IGNORECASE)


def parse_choices_from_text(text):
    """Extract [N] label options from an assistant turn.

    Only option lines that start with [N] count (not inline mentions like
    "[1]/[2]/[3]" inside a sentence). Duplicate numbers are collapsed.
    """
    options = []
    seen = set()
    for match in OPTION_RE.finditer(text or ""):
        n = int(match.group(1))
        if n in seen:
            continue
        label = match.group(2).strip().rstrip("。.,，！!？?")
        if label:
            seen.add(n)
            options.append({"n": n, "label": label[:60]})
        if len(options) >= 4:
            break
    return options


def store_choices(options, title):
    """Persist the latest offered options (overwrite; single pending choice set)."""
    if not options:
        return
    data = {
        "ts": time.time(),
        "thread": title or "",
        "options": options,
    }
    os.makedirs(QUEUE_DIR, exist_ok=True)
    with open(CHOICES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    print("[choices] stored %d options for %s" % (len(options), title or "?"), flush=True)


def load_choices():
    try:
        with open(CHOICES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    if time.time() - data.get("ts", 0) > CHOICE_TTL:
        return None
    return data


def translate_choice(text):
    """If the user's message looks like a choice and fresh options exist,
    return an explicit choice sentence, else None."""
    text = (text or "").strip()
    data = load_choices()
    if not data:
        return None
    options = {o["n"]: o["label"] for o in data.get("options", [])}
    n = None
    m = CHOICE_NUM_RE.match(text)
    if m:
        n = int(m.group(2))
    else:
        m = CHOICE_LETTER_RE.match(text)
        if m:
            n = ord(m.group(1).upper()) - ord("A") + 1
    if n is not None and n in options:
        return "用户选择选项 %d：%s" % (n, options[n])
    return None


# ---------------------------------------------------------------------------
# NapCat OneBot send / receive
# ---------------------------------------------------------------------------

def extract_text(msg):
    parts = []
    for seg in msg.get("message", []):
        if seg.get("type") == "text" and seg.get("data", {}).get("text"):
            parts.append(seg["data"]["text"])
    return "\n".join(parts).strip()


def extract_message(msg):
    """Return (text, [file_data]) from a message."""
    parts = []
    files = []
    for seg in msg.get("message", []):
        if seg.get("type") == "text" and seg.get("data", {}).get("text"):
            parts.append(seg["data"]["text"])
        elif seg.get("type") in ("file", "image"):
            files.append(seg.get("data", {}))
    return "\n".join(parts).strip(), files


def save_received_file(fdata):
    """Save a received QQ file/image into qq-files. Falls back to NapCat's
    get_file API when the message only carries a file_id."""
    def ensure_dir():
        os.makedirs(QQ_FILES_DIR, exist_ok=True)

    def unique_dst(name):
        name = name or ("qq_file_%d" % int(time.time()))
        dst = os.path.join(QQ_FILES_DIR, name)
        if os.path.exists(dst):
            base, ext = os.path.splitext(name)
            dst = os.path.join(QQ_FILES_DIR, "%s_%d%s" % (base, int(time.time()), ext))
        return dst

    src = fdata.get("file") or fdata.get("url") or ""
    name_hint = os.path.basename(src) if src else None
    try:
        # 1) local path
        if src and os.path.isfile(src):
            ensure_dir()
            dst = unique_dst(name_hint)
            import shutil
            shutil.copy2(src, dst)
            return dst
        # 2) direct http url
        if src and src.startswith("http"):
            ensure_dir()
            dst = unique_dst(name_hint)
            import urllib.request
            urllib.request.urlretrieve(src, dst)
            return dst
        # 3) resolve file_id through NapCat get_file
        fid = fdata.get("file_id") or src
        if fid:
            info = napcat_get_file(fid)
            if info:
                local = info.get("file")
                url = info.get("url")
                name = name_hint or info.get("file_name")
                if local and os.path.isfile(local):
                    ensure_dir()
                    dst = unique_dst(name)
                    import shutil
                    shutil.copy2(local, dst)
                    return dst
                if url and url.startswith("http"):
                    ensure_dir()
                    dst = unique_dst(name)
                    import urllib.request
                    urllib.request.urlretrieve(url, dst)
                    return dst
    except Exception as exc:
        print("[file] save error: %s" % exc, flush=True)
        return None
    return None


def napcat_get_file(fid):
    """Ask NapCat to resolve a file_id into a local path or download url."""
    import websocket
    try:
        ws = websocket.create_connection(WS_URL, timeout=40)
        payload = {"action": "get_file", "params": {"file": fid}, "echo": "codex-get-file"}
        ws.send(json.dumps(payload, ensure_ascii=False))
        deadline = time.time() + 40
        while time.time() < deadline:
            raw = ws.recv()
            try:
                data = json.loads(raw)
            except Exception:
                continue
            if data.get("echo") == "codex-get-file":
                ws.close()
                if data.get("status") == "ok":
                    return data.get("data") or {}
                return None
        ws.close()
    except Exception as exc:
        print("[file] get_file error: %s" % exc, flush=True)
    return None


def send(text, to):
    import websocket
    ws = websocket.create_connection(WS_URL, timeout=15)
    payload = {
        "action": "send_private_msg",
        "params": {"user_id": int(to), "message": text},
        "echo": "codex-send",
    }
    ws.send(json.dumps(payload, ensure_ascii=False))
    deadline = time.time() + 15
    while time.time() < deadline:
        raw = ws.recv()
        try:
            data = json.loads(raw)
        except Exception:
            continue
        if data.get("echo") == "codex-send":
            status = data.get("status")
            print("[send] result: %s" % status, flush=True)
            ws.close()
            return 0 if status in ("ok", "async") else 1
    ws.close()
    print("[send] timeout", flush=True)
    return 1


def push_to_owner(text):
    """Push a (possibly long) message to the owner's QQ, chunked."""
    if not text:
        return
    chunk = 1400
    for i in range(0, len(text), chunk):
        try:
            send(text[i:i + chunk], OWNER_QQ)
        except Exception as exc:
            # A transient NapCat outage (e.g. during restart) must not kill
            # the watcher thread; log and continue with the next chunk.
            print("[push] send failed: %s" % exc, flush=True)


def send_file(path, to):
    """Send a local file to a QQ user via NapCat upload_private_file."""
    import websocket
    if not os.path.isfile(path):
        print("[send-file] not found: %s" % path, flush=True)
        return 1
    ws = websocket.create_connection(WS_URL, timeout=60)
    payload = {
        "action": "upload_private_file",
        "params": {"user_id": int(to), "file": path, "name": os.path.basename(path)},
        "echo": "codex-send-file",
    }
    ws.send(json.dumps(payload, ensure_ascii=False))
    deadline = time.time() + 60
    while time.time() < deadline:
        raw = ws.recv()
        try:
            data = json.loads(raw)
        except Exception:
            continue
        if data.get("echo") == "codex-send-file":
            status = data.get("status")
            print("[send-file] result: %s" % status, flush=True)
            ws.close()
            return 0 if status == "ok" else 1
    ws.close()
    print("[send-file] timeout", flush=True)
    return 1


# ---------------------------------------------------------------------------
# conversation parsing
# ---------------------------------------------------------------------------

def split_turns(text):
    """Split conversation text into turn chunks on the 你说:/ChatGPT 说: markers."""
    import re
    parts = re.split(r"(?=你说：|ChatGPT 说：)", text)
    turns = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if part.startswith("你说：") or part.startswith("ChatGPT 说："):
            turns.append(part)
        else:
            if turns:
                turns[-1] += "\n" + part
            else:
                turns.append(part)
    return turns


def is_user_turn(turn):
    return turn.startswith("你说：")


def is_assistant_turn(turn):
    return turn.startswith("ChatGPT 说：")


def visible_threads(items):
    """Return (visible_titles, raw_indices) with fixed sidebar items removed
    and duplicate titles collapsed, so #会话 and #切 stay consistent."""
    seen = set()
    titles = []
    indices = []
    for i, title in enumerate(items):
        title = (title or "").strip()
        if not title or title in FIXED_SIDEBAR:
            continue
        if title in seen:
            continue
        seen.add(title)
        titles.append(title)
        indices.append(i)
    return titles, indices


# ---------------------------------------------------------------------------
# CDP operations
# ---------------------------------------------------------------------------

def open_cdp():
    last = None
    for _ in range(3):
        try:
            return cdp.CdpSession()
        except Exception as exc:
            last = exc
            time.sleep(1.0)
    raise last


def inject_text(sess, text):
    """Write text into the composer. Returns (ok, detail)."""
    state = sess.evaluate(cdp.GET_EDITOR_STATE_JS)
    try:
        state = json.loads(state)
    except Exception:
        return False, "编辑器状态不可读"
    if not state.get("found"):
        return False, "未找到输入框"
    if state.get("text"):
        return False, "桌面输入框有未发送内容，未注入（%s...）" % state["text"][:30]
    got = sess.evaluate(cdp.set_editor_text_js(text))
    if got != text:
        return False, "文本写入失败（%r）" % str(got)[:40]
    return True, "已写入"


def press_enter(sess):
    sess.dispatch_key("Enter", "Enter", 13)


def get_current_info(sess):
    title = sess.evaluate(cdp.GET_TITLE_JS)
    conv = sess.evaluate(cdp.GET_CONVERSATION_JS)
    return title, conv


def list_threads(sess, limit=20):
    js = (
        "(()=>{const nav=document.querySelector('nav');if(!nav)return '[]';"
        "const items=Array.from(nav.querySelectorAll('[role=listitem]')).map(e=>"
        "(e.innerText||'').trim().split('\\n')[0]).filter(Boolean);"
        "return JSON.stringify(items.slice(0," + str(limit) + "))})()"
    )
    try:
        raw = sess.evaluate(js)
        return json.loads(raw)
    except Exception:
        return []


def switch_thread(sess, index):
    js = (
        "(()=>{const nav=document.querySelector('nav');if(!nav)return 'NO_NAV';"
        "const items=Array.from(nav.querySelectorAll('[role=listitem]'));"
        "const el=items[" + str(index) + "];if(!el)return 'NO_ITEM';"
        "const btn=el.querySelector('[role=button]')||el;btn.click();return 'OK'})()"
    )
    return sess.evaluate(js)


def _resolve_rollout_path(rollout):
    """Map legacy C-dot-codex paths to the current D-drive .codex dir."""
    if not rollout:
        return None
    if os.path.isfile(rollout):
        return rollout
    legacy = LEGACY_CODEX_DIR
    if legacy and rollout.lower().startswith(legacy.lower()):
        codex_dir = CFG["codex"].get("codex_dir") or os.path.expanduser("~/.codex")
        alt = os.path.join(codex_dir, rollout[len(legacy):].lstrip("\\/"))
        if os.path.isfile(alt):
            return alt
    return None


def _session_meta_info(rollout):
    """Read session_meta from the first rollout line.
    Returns (session_id, is_old_format). is_old_format marks VS Code-era
    rollouts that have session_meta but no session_id (they are kept on disk
    for the desktop app but not shown in the QQ #会话 list)."""
    rollout = _resolve_rollout_path(rollout)
    if not rollout:
        return None, False
    try:
        with open(rollout, "r", encoding="utf-8") as f:
            line = f.readline()
        if not line:
            return None, False
        obj = json.loads(line)
        if obj.get("type") == "session_meta":
            sid = obj.get("payload", {}).get("session_id")
            if sid:
                return sid, False
            return None, True
    except Exception:
        pass
    return None, False


def list_threads_sqlite(limit=30):
    """Stable conversation list. Groups by rollout session_id (one entry per
    conversation window), applies manual renames from session_index.jsonl."""
    name_map = {}
    idx_path = os.path.join(os.path.dirname(THREADS_DB), "session_index.jsonl")
    try:
        with open(idx_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                tid = obj.get("id")
                name = obj.get("thread_name")
                ts = obj.get("updated_at") or ""
                if tid and name and (tid not in name_map or ts > name_map[tid][1]):
                    name_map[tid] = (name, ts)
    except Exception:
        pass
    db = sqlite3.connect(r"file:%s?mode=ro" % THREADS_DB, uri=True)
    try:
        cur = db.cursor()
        cur.execute(
            "SELECT t.id, t.title, t.rollout_path, "
            "COALESCE(t.updated_at_ms, t.updated_at*1000) FROM threads t "
            "WHERE t.archived=0 AND t.title IS NOT NULL AND t.title != '' "
            "AND t.title NOT LIKE '你是运行在用户 Windows 本机上的 Codex 远程代理%' "
            "AND t.title NOT LIKE 'The following is the Codex agent history%' "
            "AND t.id NOT IN (SELECT child_thread_id FROM thread_spawn_edges) "
            "ORDER BY COALESCE(t.updated_at_ms, t.updated_at) DESC"
        )
        groups = {}
        for tid, title, rollout, updated in cur.fetchall():
            if not _resolve_rollout_path(rollout):
                # record exists but its rollout file is gone: keep data, skip list
                continue
            sid, is_old = _session_meta_info(rollout)
            if is_old:
                # legacy VS Code-era thread: keep on disk, do not list on QQ
                continue
            sid = sid or tid
            rec = (tid, title, rollout, updated)
            if sid not in groups:
                groups[sid] = rec
            else:
                best = groups[sid]
                # prefer the window-root thread (id == session_id), else newest
                if best[0] != sid and (tid == sid or updated > best[3]):
                    groups[sid] = rec
        ordered = sorted(groups.values(), key=lambda r: r[3], reverse=True)
        titles = []
        for tid, title, rollout, updated in ordered:
            display = name_map.get(tid, (title, ""))[0]
            titles.append(display)
            if len(titles) >= limit:
                break
        return titles
    finally:
        db.close()


def click_thread_by_keyword(sess, kw, exact=False):
    """Scroll the sidebar and click the single listitem whose first line
    contains the keyword. Returns FOUND:<title> / MULTI:<n> / NOTFOUND / ..."""
    payload = json.dumps(kw, ensure_ascii=False)
    reset_scroll_js = (
        "(()=>{const nav=document.querySelector('nav');"
        "const sc=nav?nav.querySelector('div[class*=scroll],div[style*=overflow]')||nav:null;"
        "if(sc)sc.scrollTop=0;return 'OK'})()"
    )
    if exact:
        js = (
            "(()=>{const nav=document.querySelector('nav');if(!nav)return 'NONAV';"
            "const items=Array.from(nav.querySelectorAll('[role=listitem]'))"
            ".filter(it=>!it.querySelector('[role=listitem]'));"
            "const exact=[],prefix=[];"
            "for(const it of items){const t=(it.innerText||'').trim().split('\\n')[0].trim();"
            "if(!t)continue;"
            "if(t===K){exact.push({t:t,el:it});}else if(t.startsWith(K)){prefix.push({t:t,el:it});}}"
            "let pick=exact.length===1?exact[0]:null;"
            "if(!pick&&exact.length===0&&prefix.length===1){pick=prefix[0];}"
            "if(pick){const bs=Array.from(pick.el.querySelectorAll('[role=button]'));"
            "const btn=bs.find(b=>!String(b.className||'').includes('grab'))||bs[0]||pick.el;"
            "btn.click();return 'FOUND:'+pick.t.slice(0,60);}"
            "const all=exact.concat(prefix);"
            "if(all.length>1){return 'MULTI:'+all.length+':'+"
            "all.map(c=>c.t.slice(0,18)).join('|');}"
            "const sc=nav.querySelector('div[class*=scroll],div[style*=overflow]')||nav;"
            "if(sc.scrollTop+sc.clientHeight>=sc.scrollHeight-5){return 'END';}"
            "sc.scrollTop+=300;return 'SCROLL';})()"
        ).replace("K", payload)
    else:
        js = (
            "(()=>{const nav=document.querySelector('nav');if(!nav)return 'NONAV';"
            "const items=Array.from(nav.querySelectorAll('[role=listitem]'))"
            ".filter(it=>!it.querySelector('[role=listitem]'));"
            "const cands=[];"
            "for(const it of items){const t=(it.innerText||'').trim().split('\\n')[0].trim();"
            "if(t&&t.includes(" + payload + ")){cands.push({t:t,el:it});}}"
            "if(cands.length===1){const bs=Array.from(cands[0].el.querySelectorAll('[role=button]'));"
            "const btn=bs.find(b=>!String(b.className||'').includes('grab'))||bs[0]||cands[0].el;"
            "btn.click();return 'FOUND:'+cands[0].t.slice(0,60);}"
            "if(cands.length>1){return 'MULTI:'+cands.length+':'+"
            "cands.map(c=>c.t.slice(0,18)).join('|');}"
            "const sc=nav.querySelector('div[class*=scroll],div[style*=overflow]')||nav;"
            "if(sc.scrollTop+sc.clientHeight>=sc.scrollHeight-5){return 'END';}"
            "sc.scrollTop+=300;return 'SCROLL';})()"
        )

    def search():
        sess.evaluate(reset_scroll_js)
        time.sleep(0.5)
        for _ in range(80):
            res = sess.evaluate(js)
            if res.startswith(("FOUND:", "MULTI:", "NONAV", "END")):
                return res
            time.sleep(0.12)
        return "NOTFOUND"

    result = search()
    if result in ("END", "NOTFOUND"):
        # expand folders that look collapsed (no child items rendered)
        expand_js = (
            "(()=>{const nav=document.querySelector('nav');if(!nav)return 0;"
            "const folders=Array.from(nav.querySelectorAll('[role=listitem]'))"
            ".filter(it=>it.querySelector('[role=listitem]'));"
            "let clicked=0;"
            "for(const f of folders){const t=(f.innerText||'').trim();"
            "const lines=t.split('\\n').filter(x=>x.trim());"
            "if(lines.length<=1){const btn=f.querySelector('[role=button]');"
            "if(btn){btn.click();clicked++;}}}"
            "return clicked})()"
        )
        try:
            expanded = sess.evaluate(expand_js)
            print("[switch] expanded %s collapsed folders" % expanded, flush=True)
        except Exception:
            pass
        time.sleep(1.0)
        result = search()
    return result


def normalize_kw(text):
    """Normalize full-width punctuation users often type with IMEs."""
    return text.translate(str.maketrans("（）【】《》", "()[]<>"))


def today_report():
    """Summarize today's threads from sqlite."""
    import datetime
    now = datetime.datetime.now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_ms = int(start.timestamp() * 1000)
    db = sqlite3.connect(r"file:%s?mode=ro" % THREADS_DB, uri=True)
    try:
        cur = db.cursor()
        cur.execute(
            "SELECT title, COALESCE(updated_at_ms, updated_at*1000) FROM threads "
            "WHERE archived=0 AND title IS NOT NULL AND title != '' "
            "AND COALESCE(updated_at_ms, updated_at*1000) >= ? "
            "ORDER BY COALESCE(updated_at_ms, updated_at*1000) DESC",
            (start_ms,),
        )
        rows = cur.fetchall()
    finally:
        db.close()
    if not rows:
        return "今天还没有会话记录。"
    lines = []
    for i, (title, ts) in enumerate(rows[:15], 1):
        hhmm = time.strftime("%H:%M", time.localtime(ts / 1000)) if ts else "?"
        lines.append("%d. [%s] %s" % (i, hhmm, (title or "")[:32]))
    return "今日会话（共 %d 个，显示前 15）：\n%s" % (len(rows), "\n".join(lines))


def do_switch(selector):
    """Switch conversation by 1-based index into the sqlite list (int) or
    title keyword (str)."""
    try:
        sess = open_cdp()
        try:
            if isinstance(selector, int):
                n = selector
                titles = list_threads_sqlite(30)
                if n < 1 or n > len(titles):
                    push_to_owner("#切 %d 超出范围（最近 %d 个会话）" % (n, len(titles)))
                    return
                kw = titles[n - 1]
                label = "第 %d 个" % n
            else:
                kw = normalize_kw(selector.strip())
                label = "「%s」" % kw
            result = click_thread_by_keyword(sess, kw, exact=isinstance(selector, int))
            if result.startswith("FOUND:"):
                # wait for the title to actually update before confirming
                deadline = time.time() + 6
                title = ""
                while time.time() < deadline:
                    time.sleep(1.0)
                    title = sess.evaluate(cdp.GET_TITLE_JS)
                    if title and title.strip():
                        break
                push_to_owner("已切换到（%s）：%s" % (label, title))
            elif result.startswith("MULTI:"):
                push_to_owner("「%s」匹配到多个会话，请用更精确的关键字，如 #切 (3)" % kw)
            elif result == "NOTFOUND" or result == "END":
                push_to_owner("没找到「%s」，可能被折叠，请在桌面窗口手动打开一次后再切" % kw)
            else:
                push_to_owner("切换失败：%s" % result)
        finally:
            sess.close()
    except Exception as exc:
        push_to_owner("【#切】CDP 不可用：%s" % exc)


# ---------------------------------------------------------------------------
# QQ message handling
# ---------------------------------------------------------------------------

def handle_message(text, qq):
    text = (text or "").strip()
    if not text:
        return
    if text.startswith("切#"):
        # tolerate the reversed form the user likes: 切#8 / 切#gptwork（3）
        text = "#切" + text[2:]
    if text == "#会话":
        try:
            titles = list_threads_sqlite(30)
            lines = []
            for i, t in enumerate(titles[:20], 1):
                lines.append("%d. %s" % (i, t[:40]))
            if not lines:
                push_to_owner("没有会话记录（sqlite 为空？）")
            else:
                push_to_owner(
                    "最近会话（按更新时间）：\n"
                    + "\n".join(lines)
                    + "\n切换：#切 编号 或 #切 标题关键词（如 #切 (3)）"
                )
        except Exception as exc:
            push_to_owner("【#会话】读取失败：%s" % exc)
        return

    if text.startswith("#切"):
        arg = text[2:].strip()
        do_switch(int(arg) if arg.isdigit() else arg)
        return

    reversed_switch = re.fullmatch(r"切#(\d+)", text)
    if reversed_switch:
        do_switch(int(reversed_switch.group(1)))
        return

    quick = re.fullmatch(r"#(\d+)", text)
    if quick:
        do_switch(int(quick.group(1)))
        return

    if text.startswith("#同步"):
        n = 5
        arg = text[3:].strip()
        if arg:
            try:
                n = int(arg)
            except ValueError:
                n = 5
        n = max(1, min(n, 30))
        try:
            sess = open_cdp()
            try:
                title, conv = get_current_info(sess)
            finally:
                sess.close()
            turns = [t for t in split_turns(conv) if is_user_turn(t) or is_assistant_turn(t)]
            recent = turns[-n:]
            if not recent:
                push_to_owner("当前会话暂无消息")
                return
            parts = ["当前会话：%s" % title]
            for t in recent:
                if is_user_turn(t):
                    parts.append("> " + t[:300])
                else:
                    parts.append(t[:500])
            push_to_owner("\n\n".join(parts))
        except Exception as exc:
            push_to_owner("【#同步】CDP 不可用：%s" % exc)
        return

    if text == "#日报":
        try:
            push_to_owner(today_report())
        except Exception as exc:
            push_to_owner("【#日报】生成失败：%s" % exc)
        return

    if text == "#新对话":
        push_to_owner(
            "请在桌面 Codex 窗口点击顶部/侧边栏的“新对话”按钮，"
            "点完直接发消息即可，我会自动跟随新会话。"
        )
        return

    if text == "#日常":
        try:
            titles = list_threads_sqlite(50)
            match = [t for t in titles if "日常" in t]
        except Exception:
            match = []
        if match:
            do_switch(match[0])
        else:
            push_to_owner(
                "还没有日常会话：请在桌面点“新对话”，第一条消息发“日常对话”，"
                "之后就能用 #日常 或 #切 日常 切换到这里。"
            )
        return

    # default: send to the current desktop conversation
    try:
        sess = open_cdp()
        try:
            title, _ = get_current_info(sess)
            choice = translate_choice(text)
            payload = choice or text
            ok, detail = inject_text(sess, payload)
            if not ok:
                append_feedback(qq, text)
                push_to_owner("【桥】未注入：%s。已写入队列兜底，桌面下一回合会处理。" % detail)
                return
            press_enter(sess)
            if choice:
                push_to_owner("已识别为选择并发送到当前会话「%s」：%s" % (title[:40], choice))
            else:
                push_to_owner("已发送到当前会话「%s」：%s" % (title[:40], text[:100]))
        finally:
            sess.close()
    except Exception as exc:
        append_feedback(qq, text)
        push_to_owner("【桥】CDP 不可用（%s），消息已入队兜底，桌面会话下回合处理。" % exc)


def poke_reply():
    """Reply to a poke (拍一拍) with a quick status summary."""
    try:
        sess = open_cdp()
        try:
            title = sess.evaluate(cdp.GET_TITLE_JS)
        finally:
            sess.close()
    except Exception:
        title = "（CDP 不可用）"
    push_to_owner(
        "【戳一戳】当前会话：%s\n"
        "可用命令：#会话 / #切 N / #同步 / #日报 / #新对话 / #日常" % title
    )


# ---------------------------------------------------------------------------
# watcher: detect new assistant replies and push them to QQ
# ---------------------------------------------------------------------------

def watch_loop(stop_event):
    last_title = None
    last_turns = []
    pending_turn = None
    pending_same = 0
    first_run = True
    while not stop_event.is_set():
        try:
            sess = open_cdp()
        except Exception:
            time.sleep(5)
            continue
        print("[watch] CDP connected", flush=True)
        try:
            while not stop_event.is_set():
                if first_run:
                    # let any in-flight reply finish before establishing the baseline
                    time.sleep(8)
                    first_run = False
                    continue
                try:
                    title = sess.evaluate(cdp.GET_TITLE_JS)
                    conv = sess.evaluate(cdp.GET_CONVERSATION_JS)
                except Exception:
                    print("[watch] connection lost, reconnecting...", flush=True)
                    break
                turns = split_turns(conv)
                if last_title is None:
                    last_title, last_turns = title, turns
                elif title != last_title:
                    print("[watch] switched to: %s" % title[:40], flush=True)
                    last_title, last_turns = title, turns
                    pending_turn, pending_same = None, 0
                elif turns != last_turns:
                    # content changed: track the latest assistant tail; the
                    # 3-sample confirmation only counts while it is stable
                    last_turns = turns
                    if is_assistant_turn(turns[-1]):
                        pending_turn = turns[-1]
                        pending_same = 0
                    else:
                        pending_turn, pending_same = None, 0
                else:
                    # stable tick: accumulate confirmation for a stable reply
                    if pending_turn and is_assistant_turn(pending_turn):
                        pending_same += 1
                        if pending_same >= 3:
                            print("[watch] new reply", flush=True)
                            options = parse_choices_from_text(pending_turn)
                            if options:
                                store_choices(options, last_title)
                            push_to_owner(pending_turn)
                            pending_turn, pending_same = None, 0
                time.sleep(POLL_INTERVAL)
        finally:
            sess.close()


# ---------------------------------------------------------------------------
# NapCat listener
# ---------------------------------------------------------------------------

def on_message(ws, message):
    try:
        data = json.loads(message)
    except Exception:
        return
    post = data.get("post_type")
    if post == "notice":
        if (
            data.get("notice_type") == "notify"
            and data.get("sub_type") == "poke"
            and str(data.get("user_id")) == OWNER_QQ
        ):
            print("[poke] from owner", flush=True)
            threading.Thread(target=poke_reply, daemon=True).start()
        return
    if post != "message":
        return
    if data.get("message_type") != "private":
        return
    qq = data.get("user_id")
    if str(qq) != OWNER_QQ:
        return
    text, files = extract_message(data)
    if text or files:
        print(
            "[msg] from %s: text=%r files=%d types=%s"
            % (qq, text[:60], len(files), [s.get("type") for s in data.get("message", [])]),
            flush=True,
        )
    for fdata in files:
        saved = save_received_file(fdata)
        if saved:
            push_to_owner("【文件】已收到并保存到：%s" % saved)
        else:
            push_to_owner("【文件】收到文件但保存失败（%s）" % str(fdata)[:100])
    if text:
        print("[msg] from %s: %s" % (qq, text[:80]), flush=True)
        threading.Thread(target=handle_message, args=(text, str(qq)), daemon=True).start()


def listen():
    import websocket
    while True:
        try:
            ws = websocket.WebSocketApp(
                WS_URL,
                on_message=on_message,
                on_open=lambda w: print("[bridge] connected to NapCat", flush=True),
                on_error=lambda w, e: print("[bridge] error:", e, flush=True),
                on_close=lambda w, c, m: print("[bridge] closed", flush=True),
            )
            ws.run_forever()
        except Exception as e:
            print("[bridge] connection failed:", e, flush=True)
        time.sleep(5)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", help="path to config.json")
    ap.add_argument("--send", help="push text to QQ")
    ap.add_argument("--send-file", help="send a local file to QQ")
    ap.add_argument("--to", help="target QQ number")
    ap.add_argument("--no-watch", action="store_true", help="disable the reply watcher")
    args = ap.parse_args()
    if args.config:
        init_config(args.config)
    if not OWNER_QQ:
        print("[config] owner_qq is empty. Copy config.example.json to config.json and fill it in.", flush=True)
        if not (args.send or args.send_file):
            sys.exit(2)
    ensure_queue()
    if args.send:
        sys.exit(send(args.send, args.to or OWNER_QQ))
    if args.send_file:
        sys.exit(send_file(args.send_file, args.to or OWNER_QQ))
    print("[bridge] listening on %s" % WS_URL, flush=True)
    stop = threading.Event()
    if not args.no_watch:
        threading.Thread(target=watch_loop, args=(stop,), daemon=True).start()
    try:
        listen()
    except KeyboardInterrupt:
        stop.set()
        print("[bridge] stopped", flush=True)
