# Codex QQ Bridge

把桌面 Codex 变成你的「随身 AI」：手机 QQ 直接遥控本机 Codex，共享同一个会话。

> 一句话亮点：**只需要 QQ 小号 + 本机 Codex，纯 API、无需 GPT 账号登录、无需魔法、无需任何陌生第三方网站。**

---

## 为什么用它（亮点）

- **纯 API 用户友好**：所有交互走公开的 OneBot 11 API 与 Chrome DevTools Protocol，不依赖任何闭源服务。
- **无需 GPT 账号登录**：不需要 OpenAI / ChatGPT 账号、不需要官方 remote control、不需要云端账号体系。Codex 用什么模型，你手机就用什么模型。
- **无需魔法**：QQ 走国内直连，NapCat 是本地进程，全程不需要代理/VPN。
- **无需陌生第三方网站**：组件只有三个——QQ（NapCat）+ 你的 Codex 桌面 + 这个 bridge。没有中间服务器，数据不出本机。
- **真正的共享会话**：手机发的消息会注入桌面当前会话并实时显示，桌面的回复自动推回手机，两边是同一个对话，不是各聊各的。
- **可选项协议**：当 Codex 给出多个方案时，手机直接回复数字/字母即可选择，无需打长指令。
- **文件互通**：手机发文件到小号 → 自动存到本机；Codex 也可以主动把文件发回手机。

## 功能一览

| 手机命令 | 作用 |
|---|---|
| `#会话` | 列出最近会话 |
| `#切 N` / `#切 关键词` | 切换会话 |
| `#同步 [N]` | 拉取当前会话最近 N 条消息 |
| `#日报` | 汇总今天更新的会话 |
| `#新对话` | 开启新会话 |
| `#日常` | 切到日常会话 |
| 普通消息 | 注入桌面当前会话并发送 |
| 戳一戳（拍一拍） | 返回当前会话标题 + 可用命令 |
| 发文件 | 自动保存到本机 `qq-files` 目录 |

## 架构

```
手机 QQ ──私聊──> QQ 小号（NapCat，本机进程）
                        │  OneBot 11 WebSocket (127.0.0.1:3001)
                        ▼
                 bridge.py（本机 Python 进程）
                        │  Chrome DevTools Protocol (127.0.0.1:9229)
                        ▼
               桌面 Codex（Codex++ 启动，带调试端口）
```

- **NapCat**：QQNT 的 OneBot 实现，负责把 QQ 消息转成标准 API（本地进程）。
- **bridge.py**：消息转发 + 会话管理 + 回复监听 + 文件收发。
- **cdp.py**：极简 CDP 客户端，读写桌面 Codex 当前会话。

## 环境要求

- Windows（NapCat 目前以 Windows 注入方式运行）
- Python 3.9+（无需任何第三方依赖以外的库；仅需要 `websocket-client`）
- 桌面 Codex（经 **Codex++** 以 `--remote-debugging-port=9229` 启动，或在任意支持 CDP 的方式下启动）
- 一个 **QQ 小号**（强烈建议不用主号，见风险声明）

## 快速开始（三步）

### 1. 准备 NapCat（一次性）

1. 从 NapCat 官方渠道获取（本仓库不打包 NapCat 本体，只负责与它通信）：
   - GitHub Releases（官方）：<https://github.com/NapNeko/NapCatQQ/releases>
   - 使用文档：<https://napneko.github.io/guide/boot/Shell.html>
   - **推荐 Windows 用户下载 `NapCat.Shell.Windows.OneKey.zip`**（无头一键包，内置 QQ + NapCat，解压后运行 `NapCatInstaller.exe` 自动配置）。
2. 按官方文档完成小号登录，确认 OneBot WS 服务监听 `127.0.0.1:3001`。

### 2. 安装 bridge

**方式 A（推荐）：一键脚本**

```powershell
.\install.ps1
```

脚本会自动：检查 Python → 安装依赖 → 生成 `config.json` 并打开编辑器 → 自检 3001/9229 端口。

**方式 B（手动）**

```powershell
# 在仓库目录执行
python -m pip install -r requirements.txt
copy config.example.json config.json
notepad config.json   # 填 owner_qq / bot_qq，路径可按需调整
```

`config.json` 字段：

| 字段 | 说明 | 默认 |
|---|---|---|
| `owner_qq` | 你本人的 QQ 号（只有它能遥控） | 必填 |
| `bot_qq` | 小号 QQ 号（NapCat 登录账号） | 必填 |
| `napcat.ws_url` | NapCat OneBot WS 地址 | `ws://127.0.0.1:3001` |
| `codex.cdp_port` | Codex 调试端口 | `9229` |
| `codex.codex_dir` | `.codex` 数据目录 | 用户目录 |
| `codex.threads_db` | 会话数据库（`state_5.sqlite`） | `.codex` 下 |
| `paths.queue_dir` | 兜底队列目录 | `.codex/feedback` |
| `paths.qq_files_dir` | 手机发来文件的保存目录 | `.codex/qq-files` |

### 3. 启动

先确保桌面 Codex 开着（CDP 9229 可访问），再：

```powershell
python bridge.py
```

无窗口启动（推荐，配合计划任务/快捷方式）：

```powershell
pythonw bridge.py
```

日志写入 `bridge.out.log` / `bridge.err.log`（位置由 `codex.log_dir` 控制）。

## 命令行工具

```powershell
# 主动推送消息到你的 QQ
python bridge.py --send "任务完成了" --to 123456

# 发送文件到你的 QQ
python bridge.py --send-file "D:\报告.pdf" --to 123456

# 指定配置文件
python bridge.py --config D:\my-config.json
```

## 常见问题

**双击快捷方式没反应 / NapCat 起不来？**
Windows 上 NapCat 注入 QQ 需要管理员权限。建议把 NapCat 的启动做成「计划任务（最高权限，交互式）」或手动以管理员运行一次，避免 UAC 卡住无窗口启动。

**手机发了消息但桌面没反应？**
1. 检查 `bridge.out.log`：有 `connected to NapCat` 说明 WS 通了；有 CDP 相关报错说明桌面 Codex 的 9229 端口不可用（确认用 Codex++ 启动，或检查端口）。
2. 桌面输入框若已有未发送内容，注入会被保护性拒绝——清空桌面输入框再试。
3. 若 CDP 不可用，消息会进入兜底队列 `queue.jsonl`，Codex 读取后仍能继续。

**`#切` 匹配到多个会话？**
输入更完整的关键词，或用 `#切 (3)` 这种带编号的形式。

## 喂给本地 agent 的复现指令

想让本地 Codex / Claude Code 复现或继续开发？直接把下面这段贴给它：

```text
请先阅读仓库 https://github.com/LOrz-3/codex-qq-bridge 的 README 和源码，
理解后告诉我：这个项目的架构与核心模块，以及你复现/扩展时第一步会做什么。
```

## 目录结构

```
codex-qq-bridge/
├── bridge.py            # 主桥接（配置化）
├── cdp.py               # 极简 CDP 客户端
├── config.example.json  # 配置模板
├── install.ps1          # 一键部署脚本（自检 + 引导）
├── requirements.txt     # 依赖（websocket-client）
├── LICENSE              # MIT
└── README.md
```

## 风险声明（务必阅读）

- **仅用小号**：NapCat / OneBot 不是 QQ 官方开放的群机器人通道，存在登录验证、掉线、账号限制甚至封号风险。请只用专门的小号，不要使用主 QQ。
- **学习研究用途**：本项目的会话遥控依赖桌面 Codex 的调试端口，属于本地自动化工具，请遵守所在机构与法律要求，勿用于骚扰、营销或任何违规用途。
- **数据不出本机**：QQ 消息经 NapCat 在本机流转，不经过本项目维护的任何服务器。

## License

MIT
