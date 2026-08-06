# -*- coding: utf-8 -*-
"""WeCom (企业微信) channel.

NOTE: WeCom does NOT support a long-connection event mode - it requires a
publicly reachable callback URL for inbound messages. That conflicts with this
project's "no public IP" promise, so this adapter implements outbound
(active push via the app API) plus an optional inbound HTTP callback server
you can expose through cloudflared/ngrok if you really need it.

Outbound only requires: corp_id, agent_id, secret. Outbound pushes work
without any public URL.
"""
import json
import os
import time
import urllib.parse
import urllib.request

from core.channel import Channel, IncomingMessage
from core.registry import register


@register
class WeComChannel(Channel):
    name = "wecom"

    def __init__(self, config=None):
        super().__init__(config)
        cfg = config or {}
        self.corp_id = cfg.get("corp_id", "")
        self.agent_id = str(cfg.get("agent_id", ""))
        self.secret = cfg.get("secret", "")
        self.owner_id = str(cfg.get("owner_id", ""))
        self.callback_token = cfg.get("callback_token", "")
        self.callback_encoding_aes_key = cfg.get("encoding_aes_key", "")
        self._token = None
        self._token_expire = 0

    # ------------------------------------------------------------------
    # auth
    # ------------------------------------------------------------------

    def _get_token(self):
        if self._token and time.time() < self._token_expire - 60:
            return self._token
        url = "https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid=%s&corpsecret=%s" % (
            urllib.parse.quote(self.corp_id), urllib.parse.quote(self.secret)
        )
        with urllib.request.urlopen(url, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("errcode", 0) != 0:
            print("[wecom] gettoken error: %s" % data, flush=True)
            return None
        self._token = data["access_token"]
        self._token_expire = time.time() + data.get("expires_in", 7200)
        return self._token

    def _call(self, method, params):
        token = self._get_token()
        if not token:
            return None
        url = "https://qyapi.weixin.qq.com/cgi-bin/%s?access_token=%s" % (method, token)
        req = urllib.request.Request(
            url, data=json.dumps(params, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("errcode", 0) != 0:
            print("[wecom] %s error: %s" % (method, data), flush=True)
            return None
        return data

    # ------------------------------------------------------------------
    # outbound
    # ------------------------------------------------------------------

    def send_text(self, to, text):
        params = {
            "touser": to,
            "msgtype": "text",
            "agentid": int(self.agent_id),
            "text": {"content": text},
            "safe": 0,
        }
        return 0 if self._call("message/send", params) else 1

    def send_file(self, to, path):
        # WeCom app messages cannot push arbitrary local files directly;
        # upload a media first (media/upload) then send as file message.
        token = self._get_token()
        if not token:
            return 1
        import mimetypes
        import uuid
        boundary = "----%s" % uuid.uuid4().hex
        with open(path, "rb") as f:
            content = f.read()
        body = (
            ("--%s\r\nContent-Disposition: form-data; name=\"media\"; filename=\"%s\"\r\n"
             "Content-Type: %s\r\n\r\n" % (boundary, os.path.basename(path), mimetypes.guess_type(path)[0] or "application/octet-stream"))
            .encode("utf-8")
        ) + content + ("\r\n--%s--\r\n" % boundary).encode("utf-8")
        url = "https://qyapi.weixin.qq.com/cgi-bin/media/upload?access_token=%s&type=file" % token
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("errcode", 0) != 0:
            print("[wecom] media upload error: %s" % data, flush=True)
            return 1
        media_id = data.get("media_id")
        params = {
            "touser": to,
            "msgtype": "file",
            "agentid": int(self.agent_id),
            "file": {"media_id": media_id},
            "safe": 0,
        }
        return 0 if self._call("message/send", params) else 1

    # ------------------------------------------------------------------
    # inbound (requires public callback URL - optional)
    # ------------------------------------------------------------------

    def start(self, handler):
        if not self.callback_token:
            print("[wecom] 企业微信需要公网回调 URL 才能收消息；未配置 callback_token，跳过接收。只支持主动推送。", flush=True)
            # keep the process alive for outbound-only usage
            import threading
            import time as _time
            while True:
                _time.sleep(3600)
        self._run_callback_server(handler)

    def _run_callback_server(self, handler):
        import http.server
        import threading
        from http.server import BaseHTTPRequestHandler

        class WeComHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                # URL verification: echostr must be decrypted - implement with
                # WXBizMsgCrypt before using in production.
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length)
                # Decrypt and parse XML here (WXBizMsgCrypt) then:
                # handler(IncomingMessage(sender=..., text=..., raw=...))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"success")

        port = int(self.config.get("callback_port", 8588))
        server = http.server.HTTPServer(("0.0.0.0", port), WeComHandler)
        print("[wecom] callback server on :%d (需公网暴露)" % port, flush=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            server.shutdown()
