# -*- coding: utf-8 -*-
"""QQ channel: OneBot 11 over WebSocket (NapCat or any compatible impl)."""
import json
import time

from core.channel import Channel, IncomingMessage
from core.registry import register


@register
class QQChannel(Channel):
    name = "qq"

    def __init__(self, config=None):
        super().__init__(config)
        self.ws_url = (self.config or {}).get("ws_url") or "ws://127.0.0.1:3001"
        self.bot_qq = str((self.config or {}).get("bot_qq") or "")

    # -- outbound --------------------------------------------------------

    def _call(self, action, params, timeout=15):
        import websocket
        ws = websocket.create_connection(self.ws_url, timeout=timeout)
        echo = "codex-%s" % action
        ws.send(json.dumps({"action": action, "params": params, "echo": echo}, ensure_ascii=False))
        deadline = time.time() + timeout
        try:
            while time.time() < deadline:
                raw = ws.recv()
                try:
                    data = json.loads(raw)
                except Exception:
                    continue
                if data.get("echo") == echo:
                    return data
        finally:
            ws.close()
        return None

    def send_text(self, to, text):
        resp = self._call("send_private_msg", {"user_id": int(to), "message": text}, timeout=15)
        if resp is None:
            print("[send] timeout", flush=True)
            return 1
        status = resp.get("status")
        print("[send] result: %s" % status, flush=True)
        return 0 if status in ("ok", "async") else 1

    def send_file(self, to, path):
        import os
        if not os.path.isfile(path):
            print("[send-file] not found: %s" % path, flush=True)
            return 1
        resp = self._call(
            "upload_private_file",
            {"user_id": int(to), "file": path, "name": os.path.basename(path)},
            timeout=60,
        )
        if resp is None:
            print("[send-file] timeout", flush=True)
            return 1
        status = resp.get("status")
        print("[send-file] result: %s" % status, flush=True)
        return 0 if status == "ok" else 1

    def resolve_file(self, fid):
        """Resolve a file_id into {file|url|file_name} via NapCat get_file."""
        resp = self._call("get_file", {"file": fid}, timeout=40)
        if resp and resp.get("status") == "ok":
            return resp.get("data") or {}
        return None

    # -- inbound ----------------------------------------------------------

    @staticmethod
    def extract_message(data):
        """Extract (text, files) from a OneBot 11 private message event."""
        text = ""
        files = []
        for seg in data.get("message", []) or []:
            typ = seg.get("type")
            d = seg.get("data", {}) or {}
            if typ == "text":
                text += d.get("text", "")
            elif typ in ("image", "record", "video", "file"):
                files.append({
                    "file": d.get("file"),
                    "url": d.get("url"),
                    "file_id": d.get("file_id"),
                    "name": d.get("name") or d.get("file"),
                    "type": typ,
                })
        return text, files

    def start(self, handler):
        import websocket
        while True:
            try:
                ws = websocket.WebSocketApp(
                    self.ws_url,
                    on_message=lambda w, m: self._on_message(w, m, handler),
                    on_open=lambda w: print("[bridge] connected to NapCat", flush=True),
                    on_error=lambda w, e: print("[bridge] error:", e, flush=True),
                    on_close=lambda w, c, m: print("[bridge] closed", flush=True),
                )
                ws.run_forever()
            except Exception as e:
                print("[bridge] connection failed:", e, flush=True)
            time.sleep(5)

    def _on_message(self, ws, message, handler):
        try:
            data = json.loads(message)
        except Exception:
            return
        post_type = data.get("post_type")
        if post_type == "message" and data.get("message_type") == "private":
            qq = data.get("user_id")
            text, files = self.extract_message(data)
            if text or files:
                print(
                    "[msg] from %s: text=%r files=%d"
                    % (qq, text[:60], len(files)),
                    flush=True,
                )
            handler(IncomingMessage(sender=qq, text=text, files=files, raw=data))
        elif post_type == "notice" and data.get("notice_type") == "notify":
            if data.get("sub_type") == "poke":
                target = data.get("target_id")
                sender = data.get("user_id")
                print("[msg] poke from %s" % sender, flush=True)
                # target the poked bot's owner; engine filters by owner anyway
                handler(IncomingMessage(sender=sender, text="#戳一戳", raw=data))
