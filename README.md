# shouhou-agent — 售后邮件自动回复系统（Phase 1）

> 当前进度：**Phase 1（里程碑 A）已完成**，未实现 Phase 2 及以后内容。
> 目标链路：`IMAP 拉邮件 → 会话合并 → LLM 分类 → 低风险直接自动回复`，并带紧急暂停开关（F9）与审计日志（F10 最小版）。

## 1. 技术栈（本阶段用到的部分）

| 组件 | 选型 |
|---|---|
| 后端 | Python 3.11+ / FastAPI / SQLAlchemy 2 |
| 数据库 | SQLite（WAL），`create_all` + seed，无迁移框架 |
| LLM | DeepSeek（`deepseek-v4-flash`），封装在 `llm/client.py`，预留 provider 替换口 |
| 邮件 | IMAP（拉取）/ SMTP（发送），同步串行处理，无任务队列 |
| 调度 | Phase 1 用 CLI 轮询循环；APScheduler 属 Phase 4 |

## 2. 目录结构（Phase 1 相关）

```
shouhou-agent/
├── .env.example            # 配置模板（复制为 .env 后填写）
├── data/                   # SQLite、附件、导出（git 忽略，运行时自动创建）
└── backend/
    ├── pyproject.toml      # 依赖与 pytest 配置
    ├── docs/prompts/       # 分类 prompt（Phase 1：classify_chargeback.md）
    ├── app/
    │   ├── main.py         # FastAPI 入口（system 接口）
    │   ├── config.py       # 全部配置来自 .env
    │   ├── cli.py          # 本地命令行工具
    │   ├── db/             # SQLite WAL + create_all + seed
    │   ├── models/         # Phase 1 需要的 7 张表
    │   ├── services/       # ingest/conversation/classifier/replier/mailer/audit
    │   ├── api/system.py   # M-19 紧急暂停开关 + healthz
    │   ├── llm/client.py   # DeepSeek 封装（mock 提供本地演示）
    │   └── core/           # 异常与日志
    └── tests/              # 45 个 pytest 用例（单元 + 集成）
```

## 3. 本地启动

### 3.1 准备虚拟环境与依赖

```bash
cd backend
python3 -m venv .venv                 # 或使用 uv：uv venv
source .venv/bin/activate
uv pip install -e '.[dev]'            # 或 pip install -e '.[dev]'
```

> 说明：本项目已锁定 FastAPI==0.115.12 / anyio==4.9.0 等稳定版本，避免新版 TestClient 兼容问题。

### 3.2 配置 `.env`

```bash
cp ../.env.example ../.env
```

编辑 `../.env`，Phase 1 必填项：

| 配置项 | 说明 |
|---|---|
| `EMAIL_USERNAME` / `EMAIL_PASSWORD` | Hostinger Titan 邮箱与应用专用密码（IMAP/SMTP 地址以 Titan 后台实际显示为准） |
| `DEEPSEEK_API_KEY` | DeepSeek API Key（`LLM_PROVIDER=deepseek` 时必填） |
| `AGENT_SERVICE_TOKEN` | 紧急暂停/恢复接口的访问令牌（Phase 2 会换成登录态） |

可选：

| 配置项 | 说明 |
|---|---|
| `LLM_PROVIDER` | `deepseek`（默认）/ `openai` / `mock`（离线演示，无需 Key） |
| `LLM_MODEL` | 默认 `deepseek-v4-flash`，以你 DeepSeek 账户实际可用模型为准 |
| `LOW_CONFIDENCE_THRESHOLD` | 分类置信度低于该值转人工（默认 0.6） |
| `CONVERSATION_WINDOW_DAYS` | 会话合并窗口（默认 7 天） |
| `SMTP_RATE_LIMIT_PER_HOUR` | 每小时发送上限，0=不限（生产建议 6） |
| `POLL_INTERVAL_SECONDS` | 轮询间隔（默认 90 秒） |

### 3.3 初始化数据库

```bash
cd backend
python -m app.cli init-db
```

会自动在 `data/app.db` 建表并写入 `system_state` 种子行。

## 4. 验证步骤

### 4.1 最快验证：无邮箱、无 API Key 的离线演示

使用内置 `mock` LLM（确定性返回），`--dry-run` 不真正发信，只打印将发送的内容：

```bash
cd backend
python -m app.cli simulate --risk low --dry-run    # 低风险 → 自动回复
python -m app.cli simulate --risk high --dry-run   # 拒付/高风险 → 转人工，不发信
python -m app.cli simulate --risk medium --dry-run # 退款请求 → 转人工（挽留属 Phase 2）
```

输出中 `action=auto_sent risk=low` 即表示低风险自动回复链路跑通；同时可在 SQLite 中查看
`emails` / `conversations` / `replies` / `audit_logs` 记录。

### 4.2 真实邮箱验证（DeepSeek Key + Titan 邮箱）

1. 在 `.env` 填好邮箱与 DeepSeek 配置。
2. 用另一个邮箱向 `EMAIL_USERNAME` 发一封**低风险英文咨询**，例如：
   `Hi, what is the size of the XL t-shirt in centimeters?`
3. 手动跑一轮拉取：

   ```bash
   cd backend
   python -m app.cli poll --once
   ```

4. 观察日志出现 `Reply id=... sent to ...`，客户邮箱收到 AI 英文回复；同一主题的后续邮件会归入同一会话。
5. 常驻运行（每 90 秒轮询）：

   ```bash
   python -m app.cli run
   ```

### 4.3 紧急暂停开关（F9）

```bash
cd backend
python -m app.cli status              # 查看状态
python -m app.cli pause --reason "老板手动暂停"   # 暂停：只拉取不处理，不自动回复
python -m app.cli resume              # 恢复
```

暂停期间新邮件保持 IMAP 未读，恢复后下一轮轮询自动补处理（不丢信、不重发已处理邮件）。

也可以走 HTTP 接口（需 `.env` 中配置 `AGENT_SERVICE_TOKEN`）：

```bash
python -m uvicorn app.main:app --port 8000
curl http://127.0.0.1:8000/api/v1/system/status
curl -X POST http://127.0.0.1:8000/api/v1/system/pause \
  -H 'Content-Type: application/json' -d '{"reason":"test"}' \
  -H 'X-Service-Token: 你的令牌'
curl -X POST http://127.0.0.1:8000/api/v1/system/resume \
  -H 'X-Service-Token: 你的令牌'
curl http://127.0.0.1:8000/api/v1/healthz
```

## 5. 运行测试

```bash
cd backend
python -m pytest
```

当前结果：`45 passed`（会话合并、分类路由、IMAP 解析、SMTP 重试/限流、端到端链路、暂停开关 API）。

## 6. Phase 1 边界（重要）

- ✅ 已实现：IMAP 拉取 + Message-ID 去重、会话合并（In-Reply-To / 主题相似度 / 7 天窗口）、DeepSeek 分类（含拒付关键词+LLM 双通道）、低风险自动回复、SMTP 重试、审计日志、紧急暂停。
- ❌ 未实现（属后续 Phase）：后台登录与审核（Phase 2）、退换货挽留（Phase 2）、高风险安抚信与工单（Phase 3）、知识库与标准 QA（Phase 3）、APScheduler 与告警（Phase 4）、Docker 部署（Phase 4）。
- ⚠️ Phase 1 对**非低风险**邮件（高/中/无法判定）的处理：完整落库 + 写审计，但**不自动回复**（Phase 2/3 补审核、工单、安抚信流程）。

详细说明与待确认问题见 `IMPLEMENTATION_NOTES.md`。
