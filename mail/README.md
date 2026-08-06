# 邮件渠道（Mail Channel）

**状态**：📋 规划中（P2，详见 [ROADMAP.md](../ROADMAP.md)）

**计划实现**：QQ 邮箱 IMAP/SMTP（Python 标准库 `imaplib` / `smtplib`，零第三方依赖）

- 入站：IMAP 轮询（默认 30–60s），仅处理发件人 = owner 的未读邮件，`Message-ID` 去重
- 出站：SMTP 发送，复用 1400 字符分块
- 前置条件：QQ 邮箱开启 IMAP/SMTP 服务，生成 16 位授权码
- 定位：官方、零封号风险、分钟级延迟；适合日报 / 通知 / 异步指令，不做实时对话承诺

**接入方式**：实现 `Channel` 接口，配置见 `config.example.json` 的 `channels.mail`。
