# shouhou-agent — 售后邮件自动回复系统（Phase 2）

> 当前进度：**Phase 2（里程碑 B）已完成**，未实现 Phase 3 及以后内容。
> 目标链路：`IMAP 拉邮件 → 会话合并 → LLM 分类 → 低风险自动回复 / 中风险进待审核 / 退换货挽留`，
> 老板通过中文后台登录审核。

## 1. 技术栈

| 组件 | 选型 |
|---|---|
| 后端 | Python 3.11+ / FastAPI / SQLAlchemy 2 / SQLite（WAL） |
| 前端 | React 18 + Vite + TypeScript + Tailwind（纯 React 自带状态） |
| LLM | DeepSeek `deepseek-v4-flash`（OpenAI 兼容，已验证可用） |
| 认证 | 登录后 httpOnly Cookie（`sid` JWT，24h，无 refresh）+ bcrypt 密码 |
| 邮件 | IMAP（拉取）/ SMTP（发送），同步串行处理，无任务队列 |
| 调度 | Phase 2 仍用 CLI 轮询循环；APScheduler 属 Phase 4 |

## 2. 目录结构

```
shouhou-agent/
├── .env.example            # 配置模板（复制为 .env 后填写，.env 不提交）
├── data/                   # SQLite、附件、导出（git 忽略，运行时自动创建）
├── backend/
│   ├── docs/prompts/       # classify_chargeback / translate_reply / retention_*
│   ├── app/
│   │   ├── main.py         # FastAPI 入口（system + auth + inbox + conversations + tickets）
│   │   ├── config.py       # 全部配置来自 .env
│   │   ├── cli.py          # init-db / poll / run / pause / resume / create-owner / simulate
│   │   ├── core/security.py# bcrypt 密码哈希 + JWT（登出吊销）
│   │   ├── api/            # auth / inbox / conversations / tickets / system
│   │   ├── services/       # ingest / conversation / classifier / replier / retention /
│   │   │                   # translator / mailer / audit
│   │   └── models/         # customers / conversations / emails / replies / tickets / users / ...
│   └── tests/              # 82 个 pytest 用例
└── frontend/               # React 中文后台
    ├── vite.config.ts      # 开发代理 /api → http://127.0.0.1:8000
    └── src/
        ├── pages/          # Login / Inbox / Tickets / ConversationDetail
        └── components/     # RiskTag / Timeline / ReplyEditor / AuthGuard / Layout
```

## 3. 本地启动

### 3.1 后端依赖

```bash
cd backend
python3 -m venv .venv                 # 或 uv venv
source .venv/bin/activate
uv pip install -e '.[dev]'            # 或 pip install -e '.[dev]'
```

### 3.2 配置 `.env`

```bash
cp .env.example .env
```

必填项：

| 配置项 | 说明 |
|---|---|
| `EMAIL_USERNAME` / `EMAIL_PASSWORD` | Titan 邮箱（`support@shoplbora.com`）与密码 |
| `DEEPSEEK_API_KEY` | DeepSeek API Key |
| `SECRET_KEY` | JWT 签名密钥（`openssl rand -hex 32` 生成） |
| `OWNER_USERNAME` / `OWNER_PASSWORD` | 后台老板账号（`init-db` 时自动建用户） |

可选：`LLM_PROVIDER`（`deepseek`/`openai`/`mock`）、`LLM_MODEL`、`SMTP_RATE_LIMIT_PER_HOUR`（生产建议 6）、
`RETURN_POLICY_TEXT`（退货处理话术，留空则回复先索要订单号）、`RETENTION_MAX_ATTEMPTS`（挽留轮次上限，默认 2）、
`COMPENSATION_MAX_USD`（补偿上限，默认 10 美元）。

### 3.3 初始化数据库并创建老板账号

```bash
cd backend
python -m app.cli init-db
# 如未在 .env 设置 OWNER_PASSWORD，或想改密码：
python -m app.cli create-owner --username boss --password '你的密码'
```

### 3.4 启动后端

```bash
cd backend
python -m uvicorn app.main:app --port 8000
```

### 3.5 启动前端

```bash
cd frontend
npm install
npm run dev            # http://localhost:5173，/api 自动代理到 8000
```

生产构建：`npm run build`（产物在 `frontend/dist`）。

## 4. 验证步骤

### 4.1 最快验证：无邮箱、无 API Key 的离线演示（Mock LLM）

```bash
cd backend
LLM_PROVIDER=mock python -m app.cli simulate --risk low --dry-run      # 低风险 → 自动回复
LLM_PROVIDER=mock python -m app.cli simulate --risk medium --dry-run   # 中风险 → 待审核草稿
LLM_PROVIDER=mock python -m app.cli simulate --risk high --dry-run     # 拒付 → 转人工，不发信
LLM_PROVIDER=mock python -m app.cli simulate --reason size --dry-run       # 尺码不符 → 换货挽留自动发
LLM_PROVIDER=mock python -m app.cli simulate --reason not_wanted --dry-run # 犹豫 → 补偿草稿进待审核
LLM_PROVIDER=mock python -m app.cli simulate --reason quality --dry-run    # 质量问题 → 不挽留，照单退换
```

然后登录后台（`http://localhost:5173`）查看收件箱与会话时间轴。

### 4.2 真实邮箱完整链路（DeepSeek + Titan）

1. `.env` 填好邮箱与 DeepSeek 配置。
2. 用另一个邮箱向 `support@shoplbora.com` 发一封**低风险英文咨询**：
   `Hi, what is the size of the XL t-shirt in centimeters?`
3. 手动跑一轮拉取：

   ```bash
   cd backend
   python -m app.cli poll --once
   ```

4. 日志出现 `Reply id=... sent to ...`，客户邮箱收到英文回复；
   后台「收件箱」出现该邮件且状态为「已发送」。
5. 常驻运行：`python -m app.cli run`。

### 4.3 中风险审核流（Phase 2 新增）

1. 发一封中风险咨询，例如询问退换货政策 → 系统生成草稿进「待审核」。
2. 后台「收件箱 → 待审核」打开会话，点「审核通过并发送」；或「驳回为草稿」后编辑再发送。

### 4.4 退换货挽留闭环（Phase 2 新增）

1. 客户来信「尺码不符想退换」→ 系统自动发出换货挽留信（不涉钱，直接发）。
2. 客户回信「OK, send the replacement」→ 系统识别为接受，发确认信，挽留结束。
3. 客户来信「犹豫/买错想退款」→ 系统生成**补偿草稿（pending_review）**，老板审核后发送。
4. 客户坚持退货且挽留轮次达上限（默认 2）→ 系统停止挽留，发退货处理回复。
5. 质量/物流损坏 → 不挽留，直接发退货处理回复。

### 4.5 紧急暂停（F9）

```bash
cd backend
python -m app.cli pause --reason "老板手动暂停"
python -m app.cli resume
```

或登录后台后调用 `POST /api/v1/system/pause`（JWT 登录态，不再用 X-Service-Token）。

## 5. 运行测试

```bash
cd backend
python -m pytest
```

当前结果：**82 passed**（含 Phase 1 全部用例 + 认证/审核流/挽留/管理 API 新用例）。

## 6. Phase 2 边界（重要）

- ✅ 已实现：登录（bcrypt + httpOnly JWT + 防爆破）、中风险待审核流（`pending_review` + 通过/驳回/编辑/发送）、
  退换货挽留闭环（原因分类 → 换货/补偿/放行 + 轮次上限 + 接受判定）、中文→英文翻译接口、人工回复发送、
  收件箱/会话时间轴/工单/回收站/拆分合并/附件下载 API、前端登录/收件箱/工单/会话详情页、CLI `create-owner` 与挽留演示。
- ❌ 未实现（属后续 Phase）：高风险安抚信与工单自动生成（Phase 3）、知识库与标准 QA（Phase 3）、
  APScheduler 与告警（Phase 4）、审计查询页与设置页（Phase 4）、Docker 部署（Phase 4）。
- ⚠️ 工单表与 API 已就绪但 Phase 2 不自动创建工单（高风险邮件仍转人工等待 Phase 3 补齐）。

详细说明与待确认问题见 `IMPLEMENTATION_NOTES.md`。
