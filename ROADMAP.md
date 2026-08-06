# Codex QQ Bridge — Roadmap

> 目标：把「手机遥控桌面 Codex 共享会话」做成一个**多渠道**项目：核心引擎与消息渠道解耦，
> 任意渠道（QQ / 邮件 / Telegram / 企业微信 / 飞书）都能接入同一套会话与指令系统。

## 阶段总览

| 阶段 | 内容 | 状态 |
|---|---|---|
| P0 | 一期：QQ 渠道（NapCat / OneBot 11），配置化 + 一键部署 | ✅ 已发布 |
| P1 | Channel 抽象层：把 QQ 逻辑抽成 adapter，核心引擎与渠道解耦 | 📋 待启动 |
| P2 | 邮件渠道（QQ 邮箱 IMAP/SMTP） | 📋 待启动 |
| P3 | Telegram 渠道（官方 Bot API） | 📋 待启动 |
| P4 | 企业微信 / 飞书（官方 API，国内正规渠道） | 💡 远期 |
| P5 | 多渠道并存 + 消息路由 + 统一 CLI（`--channel`） | 💡 远期 |

---

## P1：Channel 抽象层（二期核心）

**目标**：`qq/bridge.py` 里 QQ 专属逻辑（OneBot WS 收发、命令解析、文件处理）抽成独立 adapter，核心引擎只依赖统一的 `Channel` 接口。

**接口设计（草案）**

```python
class Channel:
    name: str                       # "qq" / "mail" / "telegram"

    def connect(self) -> None: ...
    def send_text(self, to: str, text: str) -> bool: ...
    def send_file(self, to: str, path: str) -> bool: ...
    def on_event(self, handler) -> None: ...   # handler(text, files, sender)
    def close(self) -> None: ...
```

**核心引擎保留**（不因渠道而变）：
- 会话管理：`#会话` / `#切` / `#同步` / `#日报` / `#新对话` / `#日常`
- CDP 连接器：读写桌面 Codex 当前会话、监听回复
- 可选项协议：回复中 `[1] [2] ...` → 存 `choices.json` → 用户回数字/字母选择
- 队列兜底：CDP 不可用时消息进 `queue.jsonl`，任务可继续消费
- 文件路由：`send_file` / 收件保存统一由引擎调度

**验收标准**
- 现有 QQ 功能行为不变（回归测试：`#会话` / 注入 / 回复推送 / 文件）。
- 新渠道只需实现 `Channel` 接口 + 一段 `channel_config` 配置，不碰引擎代码。

## P2：邮件渠道（QQ 邮箱）

**可行性**：高。Python 标准库 `imaplib` / `smtplib` 即可，零第三方依赖。

**前置条件（用户侧一次性）**
- QQ 邮箱开启 IMAP/SMTP 服务，生成 **16 位授权码**（非登录密码）。
- 服务器：IMAP `imap.qq.com:993`（SSL），SMTP `smtp.qq.com:465`（SSL）。

**设计**
- 入站：IMAP 轮询收件箱（默认 30–60s 间隔），只处理 `发件人 = owner` 的未读邮件；`subject`/正文首段作为指令文本；附件保存到 `qq-files` 同级目录。
- 出站：SMTP 发送，主题带会话/来源标识，正文即回复内容（复用 1400 字符分块）。
- 去重：用 `Message-ID` + 已读标记，避免重复消费。
- 交互语义：邮件天然异步——**命令**（`#会话` 等）照常响应；**回复推送**在 watcher 检测到新回复时由 SMTP 发出。
- 延迟预期：分钟级，明确标注"不适合实时对话"。

**配置草案**

```json
"channels": {
  "mail": {
    "imap_host": "imap.qq.com", "imap_port": 993,
    "smtp_host": "smtp.qq.com", "smtp_port": 465,
    "user": "xxx@qq.com", "auth_code": "16位授权码",
    "poll_interval_sec": 60
  }
}
```

**验收标准**
- 从 owner 邮箱发 `#会话` → 收到会话列表邮件。
- Codex 有新回复 → owner 邮箱收到回复邮件。
- 附件双向：邮件附件落地 / 引擎可发文件。

## P3：Telegram 渠道

**可行性**：高。官方 Bot API，实时，`inline_keyboard` 可把可选项协议升级为**按钮点击**。

**前置条件（用户侧一次性）**
- 用 @BotFather 创建 bot，拿 `token`（10 分钟内可完成）。
- 手机安装 Telegram（国内需魔法——该渠道面向海外用户；不影响国内 QQ 渠道使用）。

**设计**
- 入站：`getUpdates` 长轮询（`offset` 去重），支持 `/start` 初始化与普通消息。
- 出站：`sendMessage`（支持 Markdown/HTML）；文件用 `sendDocument`，收文件用 `getFile` + 下载。
- 可选项：`reply_markup.inline_keyboard` 渲染 `[1] [2] ...`，用户点按钮即选择，体验优于数字回复。
- 命令语义与 QQ 完全一致（`#会话` 等），只换传输层。

**配置草案**

```json
"channels": {
  "telegram": {
    "bot_token": "123456:ABC-...",
    "owner_id": "你的 telegram user id"
  }
}
```

**验收标准**
- owner 发 `/会话` → 收到会话列表。
- 回复中出现选项 → 渲染成按钮，点击即选择。
- 文件双向收发正常。

## P4：企业微信 / 飞书（远期）

- 企业微信：10 人及以下个人/小团队官方机器人 API（消息/文档/日程 MCP 能力）。
- 飞书：个人可创建自建应用 + 机器人，官方 API。
- 两者均为国内官方正规渠道，作为"零风险长期方案"评估。

## P5：多渠道并存（远期）

- `config.json` 支持 `channels` 多段并存，引擎同时连接多个渠道。
- 消息路由：`owner` 在任意渠道的指令都能控制同一会话；回复按"来源渠道"回推。
- 统一 CLI：`python bridge.py --channel qq` / `--channel mail` / `--channel telegram` / `--all`。

---

## 本期交付清单（P0 已完成）

- [x] 配置化改造（`config.json` + `--config`）
- [x] 一键部署 `install.ps1`
- [x] README（亮点 / 架构 / 快速开始 / FAQ / 风险声明 / agent 复现指令）
- [x] LICENSE (MIT) / `.gitignore` / `requirements.txt`
- [x] 发布到 GitHub（public）

## 风险与约束

- QQ 渠道非官方通道：仅用小号，学习研究用途。
- 邮件渠道：延迟分钟级，只做异步/通知场景，不做实时对话承诺。
- Telegram 渠道：国内需魔法，面向海外用户；本机（国内）以 QQ 渠道为主。
- 所有渠道共用一套引擎，保证「同一会话、多渠道入口」的一致性。
