# -*- coding: utf-8 -*-
"""Channel abstraction for the Codex bridge.

A Channel wraps one messaging platform (QQ / mail / Telegram / Feishu / ...).
The engine (CodexEngine) is channel-agnostic: it only calls these methods.
New platforms implement this interface and register in core/registry.py.

Inspired by the platform/agent layering of cc-connect, kept intentionally
small so each adapter stays a single file.
"""


class IncomingMessage:
    """A normalized inbound message handed to the engine."""

    def __init__(self, sender, text="", files=None, raw=None):
        self.sender = str(sender)          # platform user id of the sender
        self.text = text or ""             # plain text payload
        self.files = files or []           # list of dicts: {file|url|file_id, name?}
        self.raw = raw                     # original platform event (debug)

    def __repr__(self):
        return "IncomingMessage(sender=%s, text=%r, files=%d)" % (
            self.sender, self.text[:40], len(self.files)
        )


class Channel:
    """Interface every messaging platform adapter must implement.

    The engine drives the loop: it calls start(handler) once, then the
    adapter is responsible for calling handler(IncomingMessage) whenever a
    message arrives. Replies go out through send_text / send_file.
    """

    name = "base"

    def __init__(self, config=None):
        self.config = config or {}

    def start(self, handler):
        """Blocking loop, or spawn threads. handler(IncomingMessage)."""
        raise NotImplementedError

    def send_text(self, to, text):
        """Send a (possibly long) text message; engine handles chunking."""
        raise NotImplementedError

    def send_file(self, to, path):
        """Send a local file. Return 0 on success."""
        raise NotImplementedError

    def stop(self):
        pass
