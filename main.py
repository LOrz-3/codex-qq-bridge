# -*- coding: utf-8 -*-
"""Unified entry point for the multi-channel Codex bridge.

Usage:
  python main.py --channel qq            # run the QQ channel (default)
  python main.py --channel mail          # run the mail channel
  python main.py --channel telegram      # run the telegram channel
  python main.py --channel all           # run every configured channel
  python main.py --send "hi" --to 123    # one-shot push via the default channel
  python main.py --send-file a.zip --to 123
"""
import argparse
import os
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from core.config import load_config  # noqa: E402
from core.engine import CodexEngine  # noqa: E402
from core import registry  # noqa: E402


def _import_channels(channel_names):
    """Import adapter modules so they self-register in the registry."""
    # qq is always importable (shipped with the repo)
    import qq.qq_channel  # noqa: F401
    for name in channel_names:
        if name == "qq":
            continue
        try:
            mod = __import__("%s.%s_channel" % (name, name), fromlist=["*"])
            print("[main] loaded channel module: %s" % name, flush=True)
        except ImportError as exc:
            print("[main] channel %s not available: %s" % (name, exc), flush=True)


def _enabled_channels(cfg, requested):
    """Resolve the channels to run: explicit names, 'all', or configured ones."""
    configured = [k for k, v in cfg.get("channels", {}).items() if v]
    if requested and requested != "all":
        return [requested]
    return configured or ["qq"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", default="qq", help="channel to run (qq/mail/telegram/feishu/wecom)")
    ap.add_argument("--config", help="path to config.json")
    ap.add_argument("--send", help="push text to the owner via the channel")
    ap.add_argument("--send-file", help="send a local file to the owner via the channel")
    ap.add_argument("--to", help="target user id (default: config owner)")
    ap.add_argument("--no-watch", action="store_true", help="disable the reply watcher")
    args = ap.parse_args()

    cfg, cfg_path = load_config(args.config, search_dir=HERE)
    names = _enabled_channels(cfg, args.channel)
    _import_channels(names)
    for n in names:
        if registry.get_channel(n) is None:
            print("[main] unknown channel: %s (available: %s)" % (n, ", ".join(registry.available_channels())), flush=True)
            sys.exit(2)

    if args.send or args.send_file:
        ch = registry.get_channel(names[0])
        ch_cfg = cfg.get("channels", {}).get(names[0], {})
        channel = ch(ch_cfg)
        to = args.to or cfg.get("owner")
        if not to:
            print("[main] no owner configured and no --to given", flush=True)
            sys.exit(2)
        if args.send:
            sys.exit(channel.send_text(to, args.send))
        if args.send_file:
            sys.exit(channel.send_file(to, args.send_file))

    print("[main] channels: %s" % ", ".join(names), flush=True)
    engines = []
    for n in names:
        ch_cfg = cfg.get("channels", {}).get(n, {})
        channel = registry.get_channel(n)(ch_cfg)
        engine = CodexEngine(
            channel, cfg=cfg, cfg_path=cfg_path, owner=cfg.get("owner"),
            page_title=cfg.get("codex", {}).get("page_title"),
        )
        engines.append(engine)

    if len(engines) == 1:
        engines[0].run(watch=not args.no_watch)
        return

    # multi-channel: one engine per channel, each on its own thread
    threads = []
    for eng in engines:
        t = threading.Thread(target=eng.run, kwargs={"watch": not args.no_watch}, daemon=True)
        t.start()
        threads.append(t)
    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        for eng in engines:
            eng.stop()


if __name__ == "__main__":
    main()
