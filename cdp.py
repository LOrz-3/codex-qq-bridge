# -*- coding: utf-8 -*-
"""Minimal Chrome DevTools Protocol client for the Codex desktop app.

The desktop app is started by codex-plus-plus with --remote-debugging-port=9229.
This module lets us read the current conversation and inject messages into the
composer, which is what turns the QQ bridge into a real "shared session".
"""
import json
import time
import urllib.request

import websocket

DEBUG_PORT = 9229
PAGE_TITLE = "Codex"


class CdpError(Exception):
    pass


def discover_page(timeout=4):
    """Return the main Codex page's websocket debugger URL."""
    url = "http://127.0.0.1:%d/json" % DEBUG_PORT
    req = urllib.request.Request(url, headers={"User-Agent": "codex-qq-bridge"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        pages = json.loads(resp.read().decode("utf-8"))
    for page in pages:
        if (
            page.get("type") == "page"
            and page.get("title") == PAGE_TITLE
            and "avatar-overlay" not in (page.get("url") or "")
        ):
            return page["webSocketDebuggerUrl"]
    raise CdpError("Codex page not found on CDP port %d" % DEBUG_PORT)


class CdpSession:
    """A thin wrapper over one CDP websocket connection."""

    def __init__(self, ws_url=None, connect_timeout=10):
        self.ws = websocket.create_connection(
            ws_url or discover_page(), timeout=connect_timeout
        )
        self._msg_id = 0
        self.send_json({"id": 1, "method": "Runtime.enable"})
        self._drain(1.0)

    def send_json(self, payload):
        self.ws.send(json.dumps(payload))

    def _drain(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            self.ws.settimeout(0.3)
            try:
                self.ws.recv()
            except Exception:
                pass

    def evaluate(self, expression):
        """Evaluate JS in the page and return the result value."""
        self._msg_id += 1
        mid = self._msg_id
        self.send_json(
            {
                "id": mid,
                "method": "Runtime.evaluate",
                "params": {"expression": expression, "returnByValue": True},
            }
        )
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise CdpError("CDP error: %s" % msg["error"])
                result = msg.get("result", {}).get("result", {})
                if result.get("subtype") == "error":
                    raise CdpError("page JS error: %s" % result.get("description"))
                return result.get("value")

    def dispatch_key(self, key, code, vk):
        """Send a raw key event through the Chromium input pipeline."""
        for kind in ("keyDown", "keyUp"):
            self._msg_id += 1
            self.send_json(
                {
                    "id": self._msg_id,
                    "method": "Input.dispatchKeyEvent",
                    "params": {
                        "type": kind,
                        "key": key,
                        "code": code,
                        "windowsVirtualKeyCode": vk,
                        "nativeVirtualKeyCode": vk,
                    },
                }
            )
        time.sleep(0.05)

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


# ---- page-side JS snippets (kept as plain strings) ----

GET_CONVERSATION_JS = (
    "(()=>{const el=document.querySelector('[data-thread-find-target=conversation]');"
    "return el?el.innerText:''})()"
)

GET_TITLE_JS = (
    "(()=>{const h=document.querySelector('main header');"
    "return h?(h.innerText||'').trim():''})()"
)

GET_EDITOR_STATE_JS = (
    "(()=>{const el=document.querySelector('[contenteditable=true]');"
    "if(!el)return JSON.stringify({found:false});"
    "return JSON.stringify({found:true,text:(el.innerText||'').trim()})})()"
)


def set_editor_text_js(text):
    """JS that focuses the composer, clears it and inserts text via execCommand.

    execCommand('insertText') fires the beforeinput/input events that ProseMirror
    listens to, so the text lands in the editor state (and is sendable).
    """
    payload = json.dumps(text, ensure_ascii=False)
    return (
        "(()=>{const el=document.querySelector('[contenteditable=true]');"
        "if(!el)return 'NO_INPUT';"
        "el.focus();"
        "document.execCommand('selectAll');"
        "document.execCommand('delete');"
        "document.execCommand('insertText',false," + payload + ");"
        "return (el.innerText||'').trim()})()"
    )


def clear_editor_js():
    return (
        "(()=>{const el=document.querySelector('[contenteditable=true]');"
        "if(!el)return 'NO_INPUT';"
        "el.focus();"
        "document.execCommand('selectAll');"
        "document.execCommand('delete');"
        "return (el.innerText||'').trim()})()"
    )
