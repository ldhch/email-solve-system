# AGENTS.md — 售后邮件自动回复系统（shouhou-agent）

> 本文件是本项目对所有 AI 编码助手（Codex / Claude 等）的常驻规范。**每次会话开始时自动生效，必须全文读完再动手。**

## 一、项目身份

- **项目**：售后问题邮箱自动回复系统
- **使用者**：一位不懂英文的老板（唯一业务决策人），单人使用
- **业务核心**：7×24 自动处理 30~50 封/天英文售后邮件；简单问题 AI 直接回，高风险问题先发安抚信再转人工；对客英文、后台中文
- **部署目标**：Hostinger VPS（Linux），单机 Docker Compose

## 二、权威文档（每次动手前必须先读）

| 文件 | 作用 | 裁决优先级 |
|---|---|---|
| `PRD.md` | 业务需求 | 业务行为以它为准（最高） |
| `TECH.md` | 技术方案 | 技术实现以它为准 |

- **冲突处理顺序**：`PRD.md > TECH.md`；两者仍歧义时，遵循「**宁可保守转人工，不让 AI 乱发**」的业务原则，并把该歧义记入 `IMPLEMENTATION_NOTES.md` 的待确认清单，不擅自决定。

## 三、简化约束（红线，严禁"自作主张加回来"）

本方案已刻意简化以匹配单人/小体量场景。**以下技术一律禁止引入**，即使你觉得"加上更专业"：

- ❌ **禁止 RAG / 向量库 / embedding**（Chroma、Pinecone、bge、BGE-M3、任何 embedding 模型）——知识库全文 + QA 全量直接注入 prompt
- ❌ **禁止任务队列 / worker / Celery / RQ / Dramatiq / DLQ**——同步串行处理：IMAP 拉一封处理一封
- ❌ **禁止 Alembic / 任何迁移框架**——`create_all` + seed 脚本
- ❌ **禁止 CSRF token / auth refresh 续期 / 全局限流**——httpOnly Cookie + `SameSite=Lax` + 登录防爆破即可
- ❌ **禁止 shadcn/ui / MUI / Zustand / Redux**——纯 React + Tailwind + React 自带状态
- ❌ **禁止 LangChain / LangGraph / 任何 Agent 框架**——纯 Python pipeline 顺序编排
- ❌ **禁止 Redis / Memcached / PostgreSQL / MySQL**——仅 SQLite（WAL）
- ❌ **禁止付费 SaaS**（Mailgun / SendGrid / Pinecone 等）
- ❌ **禁止 WebSocket**——后台 5s polling

## 四、编码规范

- 代码、注释、commit message **用英文**；对客界面文案**英文**；后台界面文案**中文**
- 函数式优先，简洁优于巧妙，可读性第一
- 所有配置/凭据走 `.env`（提供 `.env.example`），**禁止硬编码**密钥、地址、中间件配置
- **安全红线**：禁止在代码、文档、commit 中写入真实密码 / API key；`.env.example` 用占位符

## 五、开发顺序（严格按 TECH.md「九、开发顺序」Phase 1→4）

| Phase | 内容 | 完成标志 |
|---|---|---|
| 1 | 基础链路：拉邮件 → 分类 → 低风险自动发 + 紧急暂停 + 审计 | 低风险自动回复跑通，有 kill switch |
| 2 | 后台 + 登录 + 审核 + 翻译 + 挽留 | 老板可登录审核，挽留闭环 |
| 3 | 高风险安抚 + 知识库 + 标准 QA | 高风险自动安抚，知识库/QA 可用 |
| 4 | 告警 + 安全 + 调度 + Docker 部署 | MVP 可上线 |

**每阶段只做该 Phase，禁止顺手实现下一 Phase 的内容。**

## 六、每阶段收尾必须做（缺一不可）

1. 跑通该 Phase 的测试（`pytest`），能给出运行输出
2. 初始化 git 仓库（若尚未初始化），提交本阶段代码，commit message 描述清楚
3. 更新 `README.md`：本地启动步骤、`.env` 配置说明、验证方法
4. 写 `IMPLEMENTATION_NOTES.md`：
   - ✅ 已完成项（对照 TECH.md 模块清单）
   - ⚠️ 未决项 / 已知风险
   - ❓ 待老板确认问题清单（**一次性集中列出**，不要散落）
5. 主动说明"本阶段已完成、未做下一阶段"，等待老板确认后再继续
