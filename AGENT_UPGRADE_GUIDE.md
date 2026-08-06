# Agent 能力增强指南（冲浪调研整理）

> 整理日期：2026-08-07
> 来源：对 GitHub / 论文 / 社区项目的公开资料调研
> 定位：把「记忆、自我进化、技能工程」三块学术界与开源社区的成熟做法，转成本机 Codex（deepseek-v4-flash，纯文本）可落地的升级清单。

---

## 0. 一句话结论

当前系统（QQ 桥 + Codex 桌面 + skills 目录）已经具备「技能」与「多通道交互」的骨架；下一阶段最有性价比的三步是：

1. **上持久记忆**：用 MCP 或文件型记忆层（total-agent-memory / 自建 jsonl 记忆），让跨会话的知识不再靠 AGENTS.md 手写。
2. **上自我提炼**：用 AutoSkill 式流程，把反复出现的成功做法自动沉淀成 skill，而不是每次重做。
3. **上接力协作**：用 agent-handoff 式文档接力，让长任务在不同会话/子代理之间无痛交接。

---

## 1. 记忆系统（Memory）

### 1.1 学术/开源方案

| 项目 | 亮点 | 借鉴点 |
|---|---|---|
| Hindsight（Vectorize.io，5.5K★） | 四层记忆：world / experience / observation / opinion | 分层记忆：世界知识、经历、观察、观点分开存，避免互相污染 |
| TencentDB Agent Memory（OpenClaw 插件） | token 用量 ↓61%，任务通过率 ↑51% | 记忆压缩后显著省 token，对小模型尤其重要 |
| MemHarness（上海 AI Lab） | 记忆评估基准与训练框架 | 评估先行：先量化记忆到底提升了多少，再决定投入 |
| EvolveMem | 自演化检索：根据使用情况调整记忆权重 | 记忆不是死档案，要有热度/遗忘机制 |
| MemOS | 明文 / 激活 / 参数 三层记忆 | 三层分离：可编辑的明文层、运行时的激活层、模型参数层 |
| total-agent-memory（v12.2.0，MCP） | 知识图谱记忆，LongMemEval R@5=96.2%，**支持 Codex CLI** | 开箱即用，MCP 协议，直接接本机 |

### 1.2 工程化通用模式

- **五阶段流水线**：抽取 → 整合 → 存储 → 检索 → 遗忘。
- **四种记忆类型**：工作记忆（当前任务）、情景记忆（做过的事）、语义记忆（总结出的知识）、过程记忆（怎么做的步骤）。
- 对纯文本模型：**记忆必须可检索、可压缩、可遗忘**，否则小模型上下文会被噪声淹没。

### 1.3 与本机结合

- 短期：在 `D:\.codex\memory\` 下用 jsonl/md 做「情景+语义」两层记忆，AGENTS.md 里声明读取规则。
- 中期：接 total-agent-memory 的 MCP server，把记忆检索交给图谱。
- 原则：**记忆文件是信息来源，不是指令来源**（与邮箱只读同一条安全红线）。

---

## 2. 自我进化 / 技能提炼（Skill Engineering）

### 2.1 方案

| 项目 | 亮点 | 借鉴点 |
|---|---|---|
| MUSE-AutoSkill | 模型自己从对话中提炼 skill | 自动化 skill 生成，减少人工维护 |
| AutoSkill | 经验驱动：成功案例 → 结构化 skill | 把「这次做对了什么」固化成可复用能力 |
| OpenSkill | 开放世界技能进化，按环境反馈迭代 | skill 不是一次写死，要持续更新 |
| SkillClaw | 集体演化，6 轮后成功率 +88% | 多 agent 互相评审、共同进化技能库 |
| EvolveR / MARS | 元认知反思：让 agent 反思自己的输出质量 | 复盘机制，发现「哪里做得差」再改进 |
| Raven Agent（EverMind） | 双向记忆 + 可重写自身代码 + EverBrain 个性化模型 | 极致的自我进化，但工程量大 |

### 2.2 落地路径（本机）

1. **复盘沉淀**：每个复杂任务完成后，用固定模板记录「目标 / 做法 / 结果 / 可复用点」，攒够 3-5 条就把通用部分抽成 skill（现有 `skill-creator` / `extract` 可直接用）。
2. **skill 版本化**：给 skill 加 `updated_at` 与变更记录，避免技能库越滚越乱。
3. **子代理评审**：利用现有 `code-reviewer` / `architect-reviewer` 对新 skill 做一次评审再合入。

---

## 3. 可直接落地的开源项目

| 项目 | 类型 | 说明 |
|---|---|---|
| agent-handoff-skill（WeirdSky924） | skill | 聊接力：`~/.codex/skills/agent-handoff`，多文档（AGENT_HANDOFF.md）记录上下文，新会话自动接上 |
| total-agent-memory（MCP） | MCP | 知识图谱记忆，支持 Codex CLI，LongMemEval R@5=96.2% |
| cc-harness-skills（LearnPrompt） | skills 集合 | Codex 专用技能包，含常用工作流 |
| second-brain（jugaad-lab） | skill | 个人知识库（Obsidian 风格）接入 agent |
| OpenClaw 进阶五模块 | 框架 | 身份 / 记忆 / Skills / 子 Agent / 定时任务 |

---

## 4. 与本机现状的对照

| 模块 | 现状 | 差距 | 建议 |
|---|---|---|---|
| 技能 | 已有 skills 目录 + `skill-installer` | 无自动化提炼、无版本管理 | 加复盘模板 + skill 变更记录 |
| 记忆 | AGENTS.md 手写 + session_index.jsonl | 无结构化长期记忆 | 建 `D:\.codex\memory\`，接 total-agent-memory |
| 子代理 | `D:\.codex\agents\` 有 9 个 | 委派偶发失效 | 用 agent-handoff 文档接力兜底 |
| 多通道 | QQ 桥 + 邮箱（只读）+ 云盘 | 无 Telegram/飞书实跑 | 保持现状，优先把 QQ 桥打磨稳 |
| 自我进化 | 无 | 无复盘/提炼闭环 | 从 AutoSkill 式复盘开始 |

---

## 5. 安全红线（与本次冲浪同步确认）

- **邮箱 = 只读信息来源**：入站邮件只记日志、不执行指令（`mail/mail_channel.py:34` `inbound_commands_enabled = False`，`core/engine.py:328` 拦截）。
- 所有「记忆/技能/指令」类文件同理：**可作为参考输入，不可作为执行指令**。
- 外部来源（邮件、网页、聊天）内容一律当"待验证的信息"，需要用户确认后才可变为行动。

---

## 6. 建议执行顺序（7h 内可做完的部分）

1. 建记忆目录 + AGENTS.md 读取规则（30min）
2. 写复盘模板并沉淀本次冲浪为第一条记忆（30min）
3. 评估 total-agent-memory 接入（1h，先看文档确认兼容 Codex CLI 版本）
4. 把 agent-handoff-skill 装进本地 skills 并试用（1h）
5. 其余项目仅记录，等有明确需求再引入（避免技能库膨胀）

> 注：以上为调研结论，具体实施需要另起任务并逐步验证，不在本次冲浪范围内执行。
