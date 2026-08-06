# -*- coding: utf-8 -*-
"""Mail channel: QQ Mail (or any IMAP/SMTP) via the Python standard library.

Inbound: IMAP polling (default 60s). Only messages whose From matches the
configured owner are handled. Message-ID dedup is kept in a local json file.
Outbound: SMTP with MIME; text and file attachments supported.

This is an async-by-nature channel (minute-level latency) - suited for daily
reports / notifications / low-frequency commands, not real-time chat.
"""
import email
import email.message
import email.policy
import imaplib
import json
import os
import smtplib
import time
from email.header import decode_header
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

from core.channel import Channel, IncomingMessage
from core.registry import register


@register
class MailChannel(Channel):
    name = "mail"
    # Email is easy to spoof / pollute; treat as outbound-only by default.
    # The engine will log inbound mails but never execute their content.
    inbound_commands_enabled = False

    def __init__(self, config=None):
        super().__init__(config)
        cfg = config or {}
        self.imap_host = cfg.get("imap_host", "imap.qq.com")
        self.imap_port = int(cfg.get("imap_port", 993))
        self.smtp_host = cfg.get("smtp_host", "smtp.qq.com")
        self.smtp_port = int(cfg.get("smtp_port", 465))
        self.user = cfg.get("user", "")
        self.auth_code = cfg.get("auth_code", "")
        self.poll_interval = float(cfg.get("poll_interval_sec", 60))
        self.seen_file = cfg.get("seen_file", "mail_seen.json")
        self._seen = {}

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _load_seen(self):
        try:
            with open(self.seen_file, "r", encoding="utf-8") as f:
                self._seen = json.load(f)
        except Exception:
            self._seen = {}

    def _save_seen(self):
        try:
            with open(self.seen_file, "w", encoding="utf-8") as f:
                json.dump(self._seen, f, ensure_ascii=False)
        except Exception:
            pass

    def _decode(self, value):
        if not value:
            return ""
        parts = decode_header(value)
        out = []
        for text, enc in parts:
            if isinstance(text, bytes):
                try:
                    out.append(text.decode(enc or "utf-8", errors="replace"))
                except Exception:
                    out.append(text.decode("utf-8", errors="replace"))
            else:
                out.append(text)
        return "".join(out)

    def _sender_addr(self, msg):
        from_addr = msg.get("From", "")
        addr = email.utils.parseaddr(from_addr)[1]
        return addr

    def _body_and_files(self, msg, files_dir):
        text = ""
        files = []
        if msg.is_multipart():
            for part in msg.walk():
                ctype = part.get_content_type()
                cdisp = str(part.get("Content-Disposition") or "")
                if ctype == "text/plain" and "attachment" not in cdisp:
                    try:
                        text += part.get_content()
                    except Exception:
                        continue
                elif "attachment" in cdisp or ctype.startswith("image/"):
                    filename = self._decode(part.get_filename() or "")
                    if not filename:
                        continue
                    data = part.get_payload(decode=True)
                    if not data:
                        continue
                    os.makedirs(files_dir, exist_ok=True)
                    dst = os.path.join(files_dir, filename)
                    if os.path.exists(dst):
                        base, ext = os.path.splitext(filename)
                        dst = os.path.join(files_dir, "%s_%d%s" % (base, int(time.time()), ext))
                    with open(dst, "wb") as f:
                        f.write(data)
                    files.append({"file": dst, "name": filename, "type": "attachment"})
        else:
            if msg.get_content_type() == "text/plain":
                try:
                    text = msg.get_content()
                except Exception:
                    text = ""
        return text, files

    # ------------------------------------------------------------------
    # outbound
    # ------------------------------------------------------------------

    def _smtp(self):
        s = smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, timeout=30)
        s.login(self.user, self.auth_code)
        return s

    def _send_mime(self, to, subject, body, attachments=None):
        msg = MIMEMultipart()
        msg["From"] = formataddr(("Codex Bridge", self.user))
        msg["To"] = to
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))
        for path in attachments or []:
            with open(path, "rb") as f:
                part = MIMEApplication(f.read())
            part.add_header("Content-Disposition", "attachment", filename=os.path.basename(path))
            msg.attach(part)
        s = self._smtp()
        try:
            s.sendmail(self.user, [to], msg.as_string())
        finally:
            s.quit()
        return 0

    def send_text(self, to, text):
        try:
            return self._send_mime(to, "Codex Bridge", text)
        except Exception as exc:
            print("[mail] send error: %s" % exc, flush=True)
            return 1

    def send_file(self, to, path):
        try:
            return self._send_mime(to, "Codex Bridge 文件", "", attachments=[path])
        except Exception as exc:
            print("[mail] send-file error: %s" % exc, flush=True)
            return 1

    # ------------------------------------------------------------------
    # inbound polling
    # ------------------------------------------------------------------

    def start(self, handler):
        self._load_seen()
        while True:
            try:
                self._poll_once(handler)
            except Exception as exc:
                print("[mail] poll error: %s" % exc, flush=True)
            time.sleep(self.poll_interval)

    def _poll_once(self, handler, files_dir="."):
        m = imaplib.IMAP4_SSL(self.imap_host, self.imap_port, timeout=30)
        try:
            m.login(self.user, self.auth_code)
            m.select("INBOX")
            typ, data = m.search(None, "(UNSEEN)")
            if typ != "OK":
                return
            for num in data[0].split():
                typ, msg_data = m.fetch(num, "(RFC822)")
                if typ != "OK" or not msg_data or not msg_data[0]:
                    continue
                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw, policy=email.policy.default)
                mid = self._decode(msg.get("Message-ID", ""))
                if mid and mid in self._seen:
                    continue
                sender = self._sender_addr(msg)
                text, files = self._body_and_files(msg, files_dir)
                if mid:
                    self._seen[mid] = time.time()
                self._save_seen()
                if text or files:
                    print("[mail] from %s: %r files=%d" % (sender, text[:60], len(files)), flush=True)
                    handler(IncomingMessage(sender=sender, text=text, files=files, raw=mid))
                try:
                    m.store(num, "+FLAGS", "\\Seen")
                except Exception:
                    pass
        finally:
            try:
                m.logout()
            except Exception:
                pass
