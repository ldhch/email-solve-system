# shouhou-agent — 售后邮件自动回复系统（Phase 4 · MVP 可上线）

> 当前进度：**Phase 4（里程碑 D）已完成，MVP 完整可上线**。
> 目标链路：`IMAP 拉邮件 → 会话合并 → LLM 分类 → 低风险/纯咨询自动回复 / 中风险进待审核 / 退换货挽留 / 高风险安抚+工单`，
> 老板通过中文后台登录审核，维护知识库与标准 QA，设置页可暂停/查看告警配置/查看审计日志；
> APScheduler 每 90 秒自动拉取，异常自动通过 Bark + 邮件告警，密钥可加密落盘，Docker Compose + Nginx 一键部署。

## 1. 技术栈

| 组件 | 选型 |
|---|---|
| 后端 | Python 3.11+ / FastAPI / SQLAlchemy 2 / SQLite（WAL）/ APScheduler |
| 前端 | React 18 + Vite + TypeScript + Tailwind（纯 React 自带状态） |
| LLM | DeepSeek `deepseek-v4-flash`（OpenAI 兼容，已验证可用） |
| 认证 | 登录后 httpOnly Cookie（`sid` JWT，24h，无 refresh）+ bcrypt 密码 |
| 邮件 | IMAP（拉取）/ SMTP（发送），同步串行处理，无任务队列 |
| 告警 | SMTP 邮件 + Bark（iOS 推送），缺省通道静默跳过 |
| 加密 | Fernet（`cryptography`）加密敏感 .env 字段落 `data/secrets.bin` |
| 部署 | 单机 Docker Compose + Nginx（HTTP 开箱即用，HTTPS 模板 + certbot） |

## 2. 目录结构

```
shouhou-agent/
├── .env.example            # 配置模板（复制为 .env 后填写，.env 不提交）
├── docker-compose.yml      # backend + frontend + 持久卷 + healthcheck + certbot(可选)
├── data/                   # SQLite、附件、secrets.bin（git 忽略）
├── backend/
│   ├── Dockerfile          # Python 3.11 slim + uvicorn（非 root）
│   ├── docs/prompts/       # classify_chargeback / translate_reply / retention_* / reassurance
│   ├── app/
│   │   ├── main.py         # FastAPI 入口（lifespan 启动 APScheduler）
│   │   ├── config.py       # 全部配置来自 .env；运行期解密 secrets.bin
│   │   ├── cli.py          # init-db / poll / run / pause / resume / create-owner / simulate / encrypt-secrets
│   │   ├── core/security.py# bcrypt + JWT + Fernet 密钥加密（M-20）
│   │   ├── api/            # auth / inbox / conversations / tickets / kb / qa-pairs / system / audit-logs
│   │   ├── services/       # ingest / conversation / classifier / replier / retention / knowledge / qa /
│   │   │                   # translator / mailer / audit / alerting / scheduler
│   │   └── models/         # customers / conversations / emails / replies / tickets / knowledge_docs /
│   │                       # qa_pairs / users / audit_logs / system_state
│   └── tests/              # 176 个 pytest 用例（含 PRD 异常场景 1-22 E2E）
└── frontend/               # React 中文后台
    ├── Dockerfile          # Node 构建静态产物 → Nginx 托管 + /api 反代
    ├── nginx.conf          # HTTP 配置
    ├── nginx-https.conf.example  # HTTPS + Let's Encrypt 模板
    └── src/
        ├── pages/          # Login / Inbox / Tickets / KnowledgeBase / QAPairs /
        │                   # ConversationDetail / Settings / AuditLogs
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
| `EMAIL_USERNAME` / `EMAIL_PASSWORD` | Titan 邮箱与密码 |
| `DEEPSEEK_API_KEY` | DeepSeek API Key |
| `SECRET_KEY` | JWT 签名密钥（`openssl rand -hex 32` 生成） |
| `ENCRYPTION_KEY` | Fernet 密钥（生成命令见 3.4，Phase 4 必填） |
| `OWNER_USERNAME` / `OWNER_PASSWORD` | 后台老板账号（`init-db` 时自动建用户） |

Phase 4 新增可选配置：

| 配置项 | 说明 |
|---|---|
| `ALERT_BARK_WEBHOOK` | Bark 推送地址（如 `https://api.day.app/你的key`），留空则 Bark 通道静默跳过 |
| `ALERT_EMAIL_TO` | 告警接收邮箱，留空则邮件通道静默跳过 |
| `DATA_DIR` | 单一数据根（SQLite / 附件 / secrets.bin），默认 `data`（相对仓库根）；Docker 由 Compose 覆盖为 `/app/data` |
| `SESSION_AUTO_CLOSE_DAYS` | 会话无活动 N 天后自动关闭（默认 30） |
| `POLL_INTERVAL_SECONDS` | 定时拉取间隔（默认 90） |

### 3.3 生成 ENCRYPTION_KEY 并加密敏感字段（M-20）

```bash
# 生成 Fernet 密钥（base64，32 字节），填入 .env 的 ENCRYPTION_KEY
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# 把 EMAIL_PASSWORD / DEEPSEEK_API_KEY / OPENAI_API_KEY / SECRET_KEY / AGENT_SERVICE_TOKEN
# 用 Fernet 加密写入 data/secrets.bin（不删除 .env 明文，保留回退）
cd backend
python -m app.cli encrypt-secrets
```

secrets.bin 路径跟随 `DATA_DIR`（本地默认 = 仓库根 `data/secrets.bin`，与 `app.db` 同目录；容器内 = `/app/data/secrets.bin`）。
运行期行为：该文件存在时自动解密并覆盖对应字段；文件不存在、`ENCRYPTION_KEY` 为空或密钥错误时，全部回退 .env 明文（本地开发/测试不受影响）。
生产上线验证解密生效后，可手动从 `.env` 删除上述敏感字段的明文（保留其它配置）。

> ⚠️ 安全红线：真实密钥/密码/API Key 禁止写入代码、文档或 git commit。`data/` 目录已被 `.gitignore` 排除。

### 3.4 初始化数据库并创建老板账号

```bash
cd backend
python -m app.cli init-db
# 如未在 .env 设置 OWNER_PASSWORD，或想改密码：
python -m app.cli create-owner --username boss --password '你的密码'
```

### 3.5 启动后端（自动带调度器）

```bash
cd backend
python -m uvicorn app.main:app --port 8000
```

FastAPI lifespan 会自动启动 APScheduler（5 个 job：90s 拉邮件、每小时关会话、每 30 分钟扫 SLA 逾期、每 30 分钟扫补偿挽留超时、每 30s 心跳）。
`python -m app.cli run`（原轮询循环）与 `python -m app.cli poll --once` 保留为手动/调试入口，生产以 scheduler 为准。

### 3.6 启动前端

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
LLM_PROVIDER=mock python -m app.cli simulate --risk medium --dry-run   # 发票/物流/改单 → 转人工
LLM_PROVIDER=mock python -m app.cli simulate --risk high --dry-run     # 拒付 → 安抚信 + 自动建工单
LLM_PROVIDER=mock python -m app.cli simulate --reason size --dry-run       # 尺码不符 → 换货挽留自动发
LLM_PROVIDER=mock python -m app.cli simulate --reason not_wanted --dry-run # 犹豫 → 补偿草稿进待审核
LLM_PROVIDER=mock python -m app.cli simulate --reason quality --dry-run    # 质量问题 → 不挽留，照单退换
```

然后登录后台（`http://localhost:5173`）查看收件箱、会话时间轴、工单、知识库、标准问答、设置与审计日志。

### 4.2 真实邮箱完整链路（DeepSeek + Titan）

1. `.env` 填好邮箱与 DeepSeek 配置。
2. 用另一个邮箱向你的客服邮箱（如 `support@your-domain.com`，即 `.env` 的 `EMAIL_USERNAME`）发一封**低风险英文咨询**：
   `Hi, what is the size of the XL t-shirt in centimeters?`
3. 手动跑一轮拉取：

   ```bash
   cd backend
   python -m app.cli poll --once
   ```

4. 日志出现 `Reply id=... sent to ...`，客户邮箱收到英文回复；后台「收件箱」出现该邮件且状态为「已发送」。
5. 常驻运行：`python -m uvicorn app.main:app --port 8000`（APScheduler 每 90s 自动拉取）。

### 4.3 中风险审核流 / 挽留闭环 / 紧急暂停 / 高风险+工单 / 知识库与 QA

与 Phase 2/3 一致，详见各阶段说明：
- 纯咨询（政策/保修/规格/用法）自动回复；发票/物流/改单转人工；`other` 中风险进「待审核」。
- 退换货挽留：尺码→换货自动发；犹豫/买错→补偿草稿待审核；质量/损坏→照单退换；轮次超限→放行退货。
- 紧急暂停：后台「设置」页一键暂停/恢复（或 `python -m app.cli pause/resume`）。
- 高风险：安抚信（承诺 24h 专人回复、不承诺赔偿）+ 自动建单（SLA=收件+24h，24×7）；同会话追问合并进原工单，不重复发安抚信。
- 知识库：上传 PDF/DOCX/MD（≤20MB）全文注入；标准 QA：命中直出标准答案。

### 4.4 Phase 4 新增验证

1. **告警**：`.env` 配置 `ALERT_BARK_WEBHOOK` / `ALERT_EMAIL_TO` 后，模拟 LLM 连续失败 5 次（或 IMAP 连续 3 轮失败），Bark/邮箱应收到告警；未配置的通道静默跳过不影响主链路。
2. **调度**：`GET /api/v1/healthz` 返回 `{"db":"ok","scheduler":"ok","uptime_sec":N}`；DB 或 scheduler 心跳超 60s 时返回 503（Docker healthcheck 使用该接口）。
3. **审计日志**：后台「审计日志」页按动作/操作人/时间筛选 + 分页；所有发送/修改/删除/登录/暂停/自动关闭/告警升级均留痕。
4. **密钥加密**：见 3.3，`encrypt-secrets` 后重启服务，日志无解密报错即说明运行期解密生效。
5. **设置页**：暂停开关、告警通道只读状态、审计日志入口。

## 5. 运行测试

```bash
cd backend
python -m pytest
```

当前结果：**176 passed**（Phase 1-3 全部用例 + 告警通道/升级、调度 job、审计查询 API、Fernet 加密回退、healthz、PRD 异常场景 1-22 E2E）。

## 6. Docker 生产部署

### 6.1 本地 Compose 验证

```bash
cp .env.example .env      # 填好必填项
docker compose up -d --build
docker compose ps         # backend/frontend 均 healthy
curl http://localhost/api/v1/healthz
# → {"db":"ok","scheduler":"ok","uptime_sec":N}
```

- 前端静态由 Nginx 托管（同域，无 CORS），`/api` 反代到 backend。
- `data/app.db`、附件、`secrets.bin` 持久化在 `appdata` 卷。
- 两个服务均配置 `restart: always` + `/api/v1/healthz` healthcheck。
- 注意：`docker-compose.yml` 为 backend 显式注入**容器内绝对路径**
  `DATA_DIR=/app/data`、`DATABASE_URL=sqlite:////app/data/app.db` 与 `ATTACHMENT_DIR=/app/data/attachments`，
  保证 SQLite、附件与 `secrets.bin`（由 `DATA_DIR` 派生，位于 `/app/data/secrets.bin`）全部落在 `appdata` 卷内（`docker compose down/up` 不丢数据）；
  `config.py` 对绝对路径原样透传，不做仓库相对路径重写。`.env` 里的相对路径仅用于本地开发。

### 6.3 容器内密钥加密（secrets.bin 位置）

容器内 `secrets.bin` 与 DB 同根，由 `DATA_DIR=/app/data` 派生，位于卷内 `/app/data/secrets.bin`：

```bash
# 先在 .env 填好 ENCRYPTION_KEY（用 3.3 的命令生成）
docker compose exec backend python -m app.cli encrypt-secrets
```

验证：`docker compose exec backend ls -l /app/data/secrets.bin`，并重启 backend 后查看日志无
解密报错。生产确认解密生效后，可手动清空 `.env` 中敏感字段明文（保留非敏感配置，如主机/端口/阈值）。
`secrets.bin` 已由 `.gitignore` 的 `data/*` 与 `backend/data/*` 规则双重忽略，禁止提交。

### 6.2 生产（Hostinger VPS，HTTPS 走 Let's Encrypt）

1. 把代码放到 VPS（`git clone` 或 scp），确认已安装 Docker + Compose 插件。
2. 准备 `.env`（含真实凭据，禁止提交）：

   ```bash
   cp .env.example .env
   # 填 APP_ENV=production、SECRET_KEY、ENCRYPTION_KEY、邮箱、DEEPSEEK_API_KEY、
   # ALERT_BARK_WEBHOOK、ALERT_EMAIL_TO、OWNER_PASSWORD 等
   ```

3. 启动并验证：

   ```bash
   docker compose up -d --build
   docker compose ps
   curl http://<服务器IP>/api/v1/healthz
   ```

4. 绑定域名并签发证书（假设域名为 `mail.example.com`，请替换真实域名）：

   - 先在 DNS 处把域名 A 记录指向 VPS IP。
   - 把 [nginx-https.conf.example](frontend/nginx-https.conf.example) 中的 `YOUR_DOMAIN` 替换为真实域名，
     替换容器内 `/etc/nginx/conf.d/default.conf`（可重建 frontend 镜像或 `docker compose exec frontend sh -c 'cat > /etc/nginx/conf.d/default.conf'`）。
   - 首次签发：

     ```bash
     docker compose run --rm certbot certonly --webroot -w /var/www/certbot \
       -d mail.example.com --email 你的邮箱 --agree-tos --no-eff-email
     ```

   - 证书签发成功后重启 frontend（加载 443 配置），并开启自动续期：

     ```bash
     docker compose up -d --build
     docker compose --profile https up -d certbot   # 每 12h 自动续期
     ```

   - HTTPS 生效后，`APP_ENV=production` 会让 JWT Cookie 带 `Secure` 属性。

5. 安全提醒：
   - 只对 80/443 开放端口；后台无公网暴露的其它端口。
   - 生产可用 `docker compose exec backend python -m app.cli encrypt-secrets` 后，手动清空 `.env` 中敏感字段明文（保留非敏感配置）。
   - 邮件发送建议保持 `SMTP_RATE_LIMIT_PER_HOUR=6`，并配置好 SPF/DKIM/DMARC 降低进垃圾箱概率。

## 7. Phase 4 边界（重要）

- ✅ 已实现：告警通道（Bark + 邮件，缺省静默跳过）、LLM 连续失败 5 次升级、IMAP 连续 3 轮失败升级、
  APScheduler 5 job（拉邮件/关会话/SLA 逾期告警/补偿挽留超时告警+自动放行/心跳）、healthz 503、
  审计全动作覆盖 + 查询 API + 审计页、Fernet 密钥加密落盘 + `encrypt-secrets`、设置页、PRD 异常场景 1-22 E2E、
  Docker Compose + Nginx + HTTPS 模板。
- ❌ 未实现（明确属于 P1 或后续）：微信/短信告警、IMAP IDLE、Shopify/ERP 对接、OCR、移动端适配、CSV 导出下载。
- ⚠️ 告警去重为进程内存态：SLA 逾期/补偿超时每个工单/草稿每次进程运行告警一次，重启后重新计数。

详细说明与待老板确认问题见 `IMPLEMENTATION_NOTES.md`。
