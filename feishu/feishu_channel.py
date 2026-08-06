# -*- coding: utf-8 -*-
"""Feishu/Lark channel using lark-oapi SDK with WebSocket long connection.

- Long connection mode: no public IP / domain / reverse proxy needed.
- Requires a self-built app on the Feishu Open Platform (personal accounts
  can create one), with bot capability and the im.message.receive_v1 event
  subscribed over long connection.
- The lark-oapi SDK is imported lazily; if it is not installed the channel
  reports a clear install hint instead of crashing the registry.
"""
import json
import os

from core.channel import Channel, IncomingMessage
from core.registry import register


@register
class FeishuChannel(Channel):
    name = "feishu"

    def __init__(self, config=None):
        super().__init__(config)
        cfg = config or {}
        self.app_id = cfg.get("app_id", "")
        self.app_secret = cfg.get("app_secret", "")
        self.owner_id = str(cfg.get("owner_id", ""))  # open_id of the owner
        self._client = None

    def _lark(self):
        if self._client is not None:
            return self._client
        try:
            import lark_oapi as lark
        except ImportError:
            raise RuntimeError(
                "feishu 渠道需要 lark-oapi SDK：pip install lark-oapi。"
                "（QQ/mail/telegram 渠道不需要此依赖）"
            )
        client = lark.Client.builder() \
            .app_id(self.app_id) \
            .app_secret(self.app_secret) \
            .log_level(lark.LogLevel.INFO) \
            .build()
        self._lark_mod = lark
        self._client = client
        return client

    # ------------------------------------------------------------------
    # outbound
    # ------------------------------------------------------------------

    def send_text(self, to, text):
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
        client = self._lark()
        req = (
            CreateMessageRequest.builder()
            .receive_id_type("open_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(to)
                .msg_type("text")
                .content(json.dumps({"text": text}, ensure_ascii=False))
                .build()
            )
            .build()
        )
        resp = client.im.v1.message.create(req)
        if not resp.success():
            print("[feishu] send error: code=%s msg=%s" % (resp.code, resp.msg), flush=True)
            return 1
        return 0

    def send_file(self, to, path):
        # Feishu requires uploading the file first (im.v1.file.create), then
        # sending a file message. Implemented here for completeness.
        from lark_oapi.api.im.v1 import (
            CreateFileRequest,
            CreateFileRequestBody,
            CreateMessageRequest,
            CreateMessageRequestBody,
        )
        client = self._lark()
        with open(path, "rb") as f:
            file_resp = client.im.v1.file.create(
                CreateFileRequest.builder()
                .file_type("stream")
                .file_name(os.path.basename(path))
                .request_body(
                    CreateFileRequestBody.builder().file(f).build()
                )
                .build()
            )
        if not file_resp.success():
            print("[feishu] file upload error: %s" % file_resp.msg, flush=True)
            return 1
        file_key = file_resp.data.file_key
        req = (
            CreateMessageRequest.builder()
            .receive_id_type("open_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(to)
                .msg_type("file")
                .content(json.dumps({"file_key": file_key}))
                .build()
            )
            .build()
        )
        resp = client.im.v1.message.create(req)
        return 0 if resp.success() else 1

    # ------------------------------------------------------------------
    # inbound (long connection)
    # ------------------------------------------------------------------

    def start(self, handler):
        lark = self._lark()

        def on_msg(data):
            try:
                event = data.event
                msg = event.message
                sender = event.sender
                msg_type = msg.message_type
                content = msg.content or ""
                try:
                    content_json = json.loads(content)
                except Exception:
                    content_json = {}
                text = ""
                if msg_type == "text":
                    text = content_json.get("text", "")
                elif msg_type == "post":
                    text = content[:500]
                else:
                    text = "[%s]" % msg_type
                sender_id = sender.sender_id.open_id or ""
                chat_id = msg.chat_id or ""
                if text or sender_id:
                    print("[feishu] from %s: %r" % (sender_id, text[:60]), flush=True)
                    handler(IncomingMessage(sender=sender_id, text=text, files=[], raw=chat_id))
            except Exception as exc:
                print("[feishu] handler error: %s" % exc, flush=True)

        dispatcher = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(on_msg)
            .build()
        )
        ws_client = lark.ws.Client(self.app_id, self.app_secret, event_handler=dispatcher, log_level=lark.LogLevel.INFO)
        print("[feishu] starting long connection...", flush=True)
        ws_client.start()
