# -*- coding: utf-8 -*-
"""CodexEngine: channel-agnostic core of the bridge.

Owns everything that does not depend on the messaging platform:
  - CDP connection to the desktop Codex app (read/write current session)
  - conversation listing / switching / syncing (sqlite + sidebar click)
  - the reply watcher (push new assistant replies to the active channel)
  - the fallback queue (when CDP is unavailable)
  - the choice protocol ([1] [2] ... -> user replies a number/letter)

A Channel implementation (qq/qq_channel.py etc.) is injected; the engine
only calls send_text / send_file and receives IncomingMessage objects.
"""
import json
import os
import re
import sqlite3
import threading
import time

from core.channel import IncomingMessage
from core.config import resolve_paths


class CodexEngine:
    name = "codex-engine"

    def __init__(self, channel, cfg=None, cfg_path=None, owner=None, page_title="Codex"):
        self.channel = channel
        self.cfg = cfg or {}
        self.cfg_path = cfg_path
        self.owner = str(owner or self.cfg.get("owner") or "")
        self.page_title = page_title or self.cfg.get("codex", {}).get("page_title") or "Codex"
        p = resolve_paths(self.cfg)
        self.queue_dir = p["queue_dir"]
        self.queue_file = p["queue_file"]
        self.choices_file = p["choices_file"]
        self.files_dir = p["files_dir"]
        self.threads_db = p["threads_db"]
        self.legacy_codex_dir = p["legacy_codex_dir"]
        self.log_dir = p["log_dir"]
        self.poll_interval = float(self.cfg.get("tuning", {}).get("poll_interval_sec") or 2.5)
        self.choice_ttl = int(self.cfg.get("tuning", {}).get("choice_ttl_minutes") or 30) * 60
        self.cdp_port = int(self.cfg.get("codex", {}).get("cdp_port") or 9229)
        self.fixed_sidebar = {"工作", "新对话", "已安排", "插件", "置顶", "最近"}
        self._stop = threading.Event()
        self._watch_thread = None
        self._cdp_module = None

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def run(self, watch=True):
        """Start the reply watcher (optional) and hand control to the channel."""
        self.ensure_queue()
        if watch:
            self._watch_thread = threading.Thread(
                target=self._watch_loop, args=(self._stop,), daemon=True
            )
            self._watch_thread.start()
        try:
            self.channel.start(self.on_message)
        finally:
            self._stop.set()

    def stop(self):
        self._stop.set()

    # ------------------------------------------------------------------
    # queue / fallback
    # ------------------------------------------------------------------

    def ensure_queue(self):
        os.makedirs(self.queue_dir, exist_ok=True)
        if not os.path.isfile(self.queue_file):
            open(self.queue_file, "a", encoding="utf-8").close()

    def append_feedback(self, sender, text, raw_ts=None):
        self.ensure_queue()
        with open(self.queue_file, "a", encoding="utf-8") as f:
            f.write(json.dumps({"from": sender, "text": text, "ts": raw_ts or time.time()}, ensure_ascii=False) + "\n")
        print("[queue] from %s: %s" % (sender, text[:60]), flush=True)

    # ------------------------------------------------------------------
    # choice protocol
    # ------------------------------------------------------------------

    def parse_choices_from_text(self, text):
        opts = []
        for m in re.finditer(r"\[(\d+)\]\s*([^\n]{1,80})", text or ""):
            n = int(m.group(1))
            label = m.group(2).strip().strip(".")
            if 1 <= n <= 20:
                opts.append((n, label))
        return opts

    def store_choices(self, options, title):
        os.makedirs(self.queue_dir, exist_ok=True)
        data = {"ts": time.time(), "title": title, "options": options}
        with open(self.choices_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

    def load_choices(self):
        try:
            with open(self.choices_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return None
        if time.time() - data.get("ts", 0) > self.choice_ttl:
            return None
        return data

    def translate_choice(self, text):
        data = self.load_choices()
        if not data:
            return None
        s = text.strip()
        for n, label in data.get("options", []):
            if s in (str(n), chr(64 + n).lower(), chr(64 + n).upper()):
                return "[%d] %s" % (n, label)
        return None

    # ------------------------------------------------------------------
    # conversation parsing
    # ------------------------------------------------------------------

    def split_turns(self, text):
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

    def is_user_turn(self, turn):
        return turn.startswith("你说：")

    def is_assistant_turn(self, turn):
        return turn.startswith("ChatGPT 说：")

    # ------------------------------------------------------------------
    # CDP
    # ------------------------------------------------------------------

    def _cdp(self):
        if self._cdp_module is None:
            from core import cdp as cdp_mod
            cdp_mod.DEBUG_PORT = self.cdp_port
            cdp_mod.PAGE_TITLE = self.page_title
            self._cdp_module = cdp_mod
        return self._cdp_module

    def open_cdp(self):
        cdp = self._cdp()
        last = None
        for _ in range(3):
            try:
                return cdp.CdpSession()
            except Exception as exc:
                last = exc
                time.sleep(1.0)
        raise last

    def inject_text(self, sess, text):
        cdp = self._cdp()
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

    def press_enter(self, sess):
        sess.dispatch_key("Enter", "Enter", 13)

    def get_current_info(self, sess):
        cdp = self._cdp()
        title = sess.evaluate(cdp.GET_TITLE_JS)
        conv = sess.evaluate(cdp.GET_CONVERSATION_JS)
        return title, conv

    # ------------------------------------------------------------------
    # sqlite conversation listing
    # ------------------------------------------------------------------

    def _resolve_rollout_path(self, rollout):
        if not rollout:
            return None
        if os.path.isfile(rollout):
            return rollout
        legacy = self.legacy_codex_dir
        if legacy and rollout.lower().startswith(legacy.lower()):
            codex_dir = self.cfg.get("codex", {}).get("codex_dir") or os.path.expanduser("~/.codex")
            alt = os.path.join(codex_dir, rollout[len(legacy):].lstrip("\\/"))
            if os.path.isfile(alt):
                return alt
        return None

    def _session_meta_info(self, rollout):
        rollout = self._resolve_rollout_path(rollout)
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

    def list_threads_sqlite(self, limit=30):
        name_map = {}
        idx_path = os.path.join(os.path.dirname(self.threads_db), "session_index.jsonl")
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
        db = sqlite3.connect(r"file:%s?mode=ro" % self.threads_db, uri=True)
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
                if not self._resolve_rollout_path(rollout):
                    continue
                sid, is_old = self._session_meta_info(rollout)
                if is_old:
                    continue
                sid = sid or tid
                rec = (tid, title, rollout, updated)
                if sid not in groups:
                    groups[sid] = rec
                else:
                    best = groups[sid]
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

    def today_report(self):
        import datetime
        now = datetime.datetime.now()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_ms = int(start.timestamp() * 1000)
        db = sqlite3.connect(r"file:%s?mode=ro" % self.threads_db, uri=True)
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

    # ------------------------------------------------------------------
    # message dispatch (channel-agnostic)
    # ------------------------------------------------------------------

    def on_message(self, msg):
        """Entry point called by the channel. msg is an IncomingMessage."""
        if not msg or not msg.sender:
            return
        if self.owner and str(msg.sender) != self.owner:
            return
        if not getattr(self.channel, "inbound_commands_enabled", True):
            # read-only / outbound-only channel: never execute commands from
            # inbound messages (spoofing / injection risk). Log and drop.
            print(
                "[engine] channel %s is outbound-only, ignoring inbound msg from %s: %r"
                % (getattr(self.channel, "name", "?"), msg.sender, (msg.text or "")[:60]),
                flush=True,
            )
            return
        # files first
        for fdata in msg.files:
            saved = self.save_received_file(fdata)
            if saved:
                self.push("【文件】已收到并保存到：%s" % saved)
            else:
                self.push("【文件】收到文件但保存失败（%s）" % str(fdata)[:100])
        if msg.text:
            threading.Thread(target=self.handle_text, args=(msg.text, str(msg.sender)), daemon=True).start()

    def handle_text(self, text, sender):
        text = (text or "").strip()
        if not text:
            return
        if text.startswith("切#"):
            text = "#切" + text[2:]
        if text == "#会话":
            try:
                titles = self.list_threads_sqlite(30)
                lines = []
                for i, t in enumerate(titles[:20], 1):
                    lines.append("%d. %s" % (i, t[:40]))
                if not lines:
                    self.push("没有会话记录（sqlite 为空？）")
                else:
                    self.push(
                        "最近会话（按更新时间）：\n" + "\n".join(lines)
                        + "\n切换：#切 编号 或 #切 标题关键词（如 #切 (3)）"
                    )
            except Exception as exc:
                self.push("【#会话】读取失败：%s" % exc)
            return
        if text.startswith("#切"):
            arg = text[2:].strip()
            self.do_switch(int(arg) if arg.isdigit() else arg)
            return
        m = re.fullmatch(r"切#(\d+)", text)
        if m:
            self.do_switch(int(m.group(1)))
            return
        m = re.fullmatch(r"#(\d+)", text)
        if m:
            self.do_switch(int(m.group(1)))
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
                sess = self.open_cdp()
                try:
                    title, conv = self.get_current_info(sess)
                finally:
                    sess.close()
                turns = [t for t in self.split_turns(conv) if self.is_user_turn(t) or self.is_assistant_turn(t)]
                recent = turns[-n:]
                if not recent:
                    self.push("当前会话暂无消息")
                    return
                parts = ["当前会话：%s" % title]
                for t in recent:
                    if self.is_user_turn(t):
                        parts.append("> " + t[:300])
                    else:
                        parts.append(t[:500])
                self.push("\n\n".join(parts))
            except Exception as exc:
                self.push("【#同步】CDP 不可用：%s" % exc)
            return
        if text == "#日报":
            try:
                self.push(self.today_report())
            except Exception as exc:
                self.push("【#日报】生成失败：%s" % exc)
            return
        if text == "#新对话":
            self.push("请在桌面 Codex 窗口点击顶部/侧边栏的“新对话”按钮，点完直接发消息即可，我会自动跟随新会话。")
            return
        if text == "#日常":
            try:
                titles = self.list_threads_sqlite(50)
                match = [t for t in titles if "日常" in t]
            except Exception:
                match = []
            if match:
                self.do_switch(match[0])
            else:
                self.push("还没有日常会话：请在桌面点“新对话”，第一条消息发“日常对话”，之后就能用 #日常 或 #切 日常 切换到这里。")
            return
        if text == "#戳一戳":
            self.poke_reply()
            return
        # default: inject into the current desktop conversation
        try:
            sess = self.open_cdp()
            try:
                title, _ = self.get_current_info(sess)
                choice = self.translate_choice(text)
                payload = choice or text
                ok, detail = self.inject_text(sess, payload)
                if not ok:
                    self.append_feedback(sender, text)
                    self.push("【桥】未注入：%s。已写入队列兜底，桌面下一回合会处理。" % detail)
                    return
                self.press_enter(sess)
                if choice:
                    self.push("已识别为选择并发送到当前会话「%s」：%s" % (title[:40], choice))
                else:
                    self.push("已发送到当前会话「%s」：%s" % (title[:40], text[:100]))
            finally:
                sess.close()
        except Exception as exc:
            self.append_feedback(sender, text)
            self.push("【桥】CDP 不可用（%s），消息已入队兜底，桌面会话下回合处理。" % exc)

    def poke_reply(self):
        """Reply to a poke (拍一拍) with a quick status summary."""
        try:
            sess = self.open_cdp()
            try:
                title = sess.evaluate(self._cdp().GET_TITLE_JS)
            finally:
                sess.close()
        except Exception:
            title = "（CDP 不可用）"
        self.push(
            "【戳一戳】当前会话：%s\n"
            "可用命令：#会话 / #切 N / #同步 / #日报 / #新对话 / #日常" % title
        )

    # ------------------------------------------------------------------
    # switching
    # ------------------------------------------------------------------

    def normalize_kw(self, text):
        return text.translate(str.maketrans("（）【】《》", "()[]<>"))

    def _click_thread_by_keyword(self, sess, kw, exact=False):
        cdp = self._cdp()
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
                "if(all.length>1){return 'MULTI:'+all.length+':'+all.map(c=>c.t.slice(0,18)).join('|');}"
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
                "if(cands.length>1){return 'MULTI:'+cands.length+':'+cands.map(c=>c.t.slice(0,18)).join('|');}"
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

    def do_switch(self, selector):
        try:
            sess = self.open_cdp()
            try:
                if isinstance(selector, int):
                    n = selector
                    titles = self.list_threads_sqlite(30)
                    if n < 1 or n > len(titles):
                        self.push("#切 %d 超出范围（最近 %d 个会话）" % (n, len(titles)))
                        return
                    kw = titles[n - 1]
                    label = "第 %d 个" % n
                else:
                    kw = self.normalize_kw(selector.strip())
                    label = "「%s」" % kw
                result = self._click_thread_by_keyword(sess, kw, exact=isinstance(selector, int))
                if result.startswith("FOUND:"):
                    deadline = time.time() + 6
                    title = ""
                    while time.time() < deadline:
                        time.sleep(1.0)
                        title = sess.evaluate(self._cdp().GET_TITLE_JS)
                        if title and title.strip():
                            break
                    self.push("已切换到（%s）：%s" % (label, title))
                elif result.startswith("MULTI:"):
                    self.push("「%s」匹配到多个会话，请用更精确的关键字，如 #切 (3)" % kw)
                elif result in ("NOTFOUND", "END"):
                    self.push("没找到「%s」，可能被折叠，请在桌面窗口手动打开一次后再切" % kw)
                else:
                    self.push("切换失败：%s" % result)
            finally:
                sess.close()
        except Exception as exc:
            self.push("【#切】CDP 不可用：%s" % exc)

    # ------------------------------------------------------------------
    # file receiving
    # ------------------------------------------------------------------

    def save_received_file(self, fdata):
        def unique_dst(name):
            name = name or ("file_%d" % int(time.time()))
            dst = os.path.join(self.files_dir, name)
            if os.path.exists(dst):
                base, ext = os.path.splitext(name)
                dst = os.path.join(self.files_dir, "%s_%d%s" % (base, int(time.time()), ext))
            return dst

        os.makedirs(self.files_dir, exist_ok=True)
        src = fdata.get("file") or fdata.get("url") or ""
        name_hint = os.path.basename(src) if src else None
        try:
            if src and os.path.isfile(src):
                import shutil
                dst = unique_dst(name_hint)
                shutil.copy2(src, dst)
                return dst
            if src and src.startswith("http"):
                import urllib.request
                dst = unique_dst(name_hint)
                urllib.request.urlretrieve(src, dst)
                return dst
            fid = fdata.get("file_id") or src
            if fid and hasattr(self.channel, "resolve_file"):
                info = self.channel.resolve_file(fid)
                if info:
                    local = info.get("file")
                    url = info.get("url")
                    name = name_hint or info.get("file_name")
                    if local and os.path.isfile(local):
                        import shutil
                        dst = unique_dst(name)
                        shutil.copy2(local, dst)
                        return dst
                    if url and url.startswith("http"):
                        import urllib.request
                        dst = unique_dst(name)
                        urllib.request.urlretrieve(url, dst)
                        return dst
        except Exception as exc:
            print("[file] save error: %s" % exc, flush=True)
            return None
        return None

    # ------------------------------------------------------------------
    # reply watcher
    # ------------------------------------------------------------------

    def _watch_loop(self, stop_event):
        last_title = None
        last_turns = []
        pending_turn = None
        pending_same = 0
        first_run = True
        while not stop_event.is_set():
            try:
                sess = self.open_cdp()
            except Exception:
                time.sleep(5)
                continue
            print("[watch] CDP connected", flush=True)
            try:
                while not stop_event.is_set():
                    if first_run:
                        time.sleep(8)
                        first_run = False
                        continue
                    try:
                        title = sess.evaluate(self._cdp().GET_TITLE_JS)
                        conv = sess.evaluate(self._cdp().GET_CONVERSATION_JS)
                    except Exception:
                        print("[watch] connection lost, reconnecting...", flush=True)
                        break
                    turns = self.split_turns(conv)
                    if last_title is None:
                        last_title, last_turns = title, turns
                    elif title != last_title:
                        print("[watch] switched to: %s" % title[:40], flush=True)
                        last_title, last_turns = title, turns
                        pending_turn, pending_same = None, 0
                    elif turns != last_turns:
                        last_turns = turns
                        if self.is_assistant_turn(turns[-1]):
                            pending_turn = turns[-1]
                            pending_same = 0
                        else:
                            pending_turn, pending_same = None, 0
                    else:
                        if pending_turn and self.is_assistant_turn(pending_turn):
                            pending_same += 1
                            if pending_same >= 3:
                                print("[watch] new reply", flush=True)
                                options = self.parse_choices_from_text(pending_turn)
                                if options:
                                    self.store_choices(options, last_title)
                                self.push(pending_turn)
                                pending_turn, pending_same = None, 0
                    time.sleep(self.poll_interval)
            finally:
                sess.close()

    # ------------------------------------------------------------------
    # push helpers
    # ------------------------------------------------------------------

    def push(self, text, to=None):
        """Send to the configured owner via the channel, chunked at 1400."""
        if not text:
            return
        to = to or self.owner
        chunk = 1400
        for i in range(0, len(text), chunk):
            try:
                self.channel.send_text(to, text[i:i + chunk])
            except Exception as exc:
                print("[push] send failed: %s" % exc, flush=True)

    def push_file(self, path, to=None):
        to = to or self.owner
        return self.channel.send_file(to, path)
