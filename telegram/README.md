# Telegram 渠道（Telegram Channel）

**状态**：📋 规划中（P3，详见 [ROADMAP.md](../ROADMAP.md)）

**计划实现**：官方 Bot API（长轮询 + 收发文件）

- 入站：`getUpdates` 长轮询（`offset` 去重）
- 出站：`sendMessage`（Markdown/HTML）、`sendDocument` 收发文件
- 可选项：`inline_keyboard` 把 `[1] [2] ...` 渲染成按钮，点击即选择
- 前置条件：@BotFather 创建 bot 拿 token
- 定位：实时、体验最佳；国内需魔法，面向海外用户，不影响国内 QQ 渠道

**接入方式**：实现 `Channel` 接口，配置见 `config.example.json` 的 `channels.telegram`。
