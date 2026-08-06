# -*- coding: utf-8 -*-
"""Telegram channel: official Bot API over long polling.

- Inbound: getUpdates long polling (offset dedup). /start initializes.
- Outbound: sendMessage (text) and sendDocument (files).
- Choices: inline_keyboard renders [1] [2] ... as clickable buttons.
Requires a bot token from @BotFather. Needs network access to api.telegram.org
(not reachable from mainland China without a proxy - designed for overseas users).
"""
import json
import os
import time
import urllib.parse
import urllib.request

from core.channel import Channel, IncomingMessage
from core.registry import register


@register
class TelegramChannel(Channel):
    name = "telegram"

    def __init__(self, config=None):
        super().__init__(config)
        cfg = config or {}
        self.token = cfg.get("bot_token", "")
        self.owner_id = str(cfg.get("owner_id", ""))
        self.poll_timeout = int(cfg.get("poll_timeout_sec", 25))
        self._offset = 0
        self._api = "https://api.telegram.org/bot%s" % self.token

    # ------------------------------------------------------------------
    # low-level
    # ------------------------------------------------------------------

    def _call(self, method, params=None, files=None):
        url = self._api + "/" + method
        data = None
        headers = {}
        if files:
            # multipart/form-data via urllib
            boundary = "----CodexBridgeBoundary%s" % int(time.time() * 1000)
            body = b""
            for key, value in (params or {}).items():
                body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (boundary, key, value)).encode("utf-8")
            for key, path in files.items():
                with open(path, "rb") as f:
                    content = f.read()
                body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\nContent-Type: application/octet-stream\r\n\r\n" % (boundary, key, os.path.basename(path))).encode("utf-8")
                body += content + b"\r\n"
            body += ("--%s--\r\n" % boundary).encode("utf-8")
            data = body
            headers["Content-Type"] = "multipart/form-data; boundary=%s" % boundary
        elif params:
            data = urllib.parse.urlencode(params).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            print("[tg] api error: %s" % exc, flush=True)
            return None
        if not payload.get("ok"):
            print("[tg] api not ok: %s" % payload, flush=True)
            return None
        return payload.get("result")

    # ------------------------------------------------------------------
    # outbound
    # ------------------------------------------------------------------

    def send_text(self, to, text):
        res = self._call("sendMessage", {"chat_id": to, "text": text})
        return 0 if res else 1

    def send_file(self, to, path):
        res = self._call("sendDocument", {"chat_id": to}, files={"document": path})
        return 0 if res else 1

    def send_buttons(self, to, text, options):
        rows = []
        for i in range(0, len(options), 3):
            row = []
            for n, label in options[i:i + 3]:
                row.append({"text": "[%d] %s" % (n, label[:40]), "callback_data": str(n)})
            rows.append(row)
        markup = json.dumps({"inline_keyboard": rows})
        res = self._call("sendMessage", {"chat_id": to, "text": text, "reply_markup": markup})
        return 0 if res else 1

    # ------------------------------------------------------------------
    # inbound
    # ------------------------------------------------------------------

    def start(self, handler):
        while True:
            try:
                updates = self._call("getUpdates", {
                    "offset": self._offset,
                    "timeout": self.poll_timeout,
                    "allowed_updates": json.dumps(["message", "callback_query"]),
                })
                if not updates:
                    continue
                for upd in updates:
                    self._offset = max(self._offset, upd.get("update_id", 0) + 1)
                    if "message" in upd:
                        self._handle_message(upd["message"], handler)
                    elif "callback_query" in upd:
                        self._handle_callback(upd["callback_query"], handler)
            except Exception as exc:
                print("[tg] poll error: %s" % exc, flush=True)
                time.sleep(3)

    def _handle_message(self, msg, handler):
        chat = msg.get("chat", {})
        chat_id = str(chat.get("id", ""))
        user = msg.get("from", {})
        sender = str(user.get("id", chat_id))
        text = msg.get("text") or msg.get("caption") or ""
        files = []
        for ftype, fkey in (("photo", "photo"), ("document", "document"), ("audio", "audio"), ("video", "video")):
            if ftype in msg:
                f = msg[ftype]
                if isinstance(f, list):
                    f = f[-1]
                fid = f.get("file_id")
                if fid:
                    files.append({"file_id": fid, "name": f.get("file_name"), "type": ftype})
        if text or files:
            print("[tg] from %s: %r files=%d" % (sender, text[:60], len(files)), flush=True)
            handler(IncomingMessage(sender=sender, text=text, files=files, raw=chat_id))

    def _handle_callback(self, cb, handler):
        user = cb.get("from", {})
        sender = str(user.get("id", ""))
        data = cb.get("data", "")
        chat = cb.get("message", {}).get("chat", {})
        try:
            self._call("answerCallbackQuery", {"callback_query_id": cb.get("id", ""), "text": "已选择 %s" % data})
        except Exception:
            pass
        handler(IncomingMessage(sender=sender, text=data, files=[], raw=chat.get("id", "")))

    def resolve_file(self, fid):
        res = self._call("getFile", {"file_id": fid})
        if not res or "file_path" not in res:
            return None
        path = res["file_path"]
        url = "https://api.telegram.org/file/bot%s/%s" % (self.token, path)
        return {"url": url, "file_name": os.path.basename(path)}
