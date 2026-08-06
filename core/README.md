# 核心引擎（Core Engine）

**状态**：📋 待启动（P1，详见 [ROADMAP.md](../ROADMAP.md)）

**目标**：把渠道无关逻辑收敛到 `core/`，各渠道只实现 `Channel` 接口，不碰引擎。

计划包含：
- `Channel` 接口定义（`send_text` / `send_file` / `on_event`）
- 会话管理（`#会话` / `#切` / `#同步` / `#日报` / `#新对话` / `#日常`）
- CDP 连接器（读写桌面 Codex 当前会话、监听回复）
- 可选项协议、队列兜底、文件路由

接入约定：`qq/`、`mail/`、`telegram/` 等渠道目录各自实现 `Channel`，配置统一放各渠道的 `config.example.json`。
