# IMPLEMENTATION_NOTES — Phase 4（里程碑 D，MVP 可上线）

> 记录日期：2026-08-15
> 范围：TECH.md 第九章 Phase 4（M-18 告警 + M-17 审计增强 + M-12 APScheduler + M-20 密钥加密 + F-09 设置页 + E2E 异常场景 1-22 + Docker/Nginx 部署）
> Phase 3 遗留的 4 个待确认问题（知识库文档规模 / QA 命中判定 / QA 上限 100 条 / 未确认标注文案）不阻塞本阶段，继续列入文末待确认清单。

## 🔧 Review 修复（2026-08-17，5 项）

| # | 问题 | 修复 | 状态 |
|---|---|---|---|
| 1（阻塞） | 会话多封低风险邮件逐封回信（刷屏），违反 PRD F2 / 异常场景 3 / TECH M-07 | `fetch_and_process` 改为两阶段：先整批 parse/去重/合并/分类/落库，低风险 auto_send 延迟；按 conversation 分组后每组只发一封聚合回复（`ReplierService.generate_aggregated`，把本轮全部新邮件正文一起注入 prompt，`in_reply_to` 指向组内最新一封）。高风险/挽留/审核/人工/静默分支保持逐封语义；仍同步串行，无队列。失败语义：SMTP 失败保留 failed 草稿、下轮重发同一草稿（其余组内邮件置已读）；LLM 生成失败删除本组已入库邮件、保持 UNSEEN，下轮完整重试（与单封回滚语义一致）。`MockLLMClient` 的拒付子串匹配误判（"sue" 命中 "issue"）同步改为词边界，与真实分类器一致 | ✅ |
| 2（阻塞） | Docker 容器内 `parents[2]` 把数据根解析到卷外（`/data/app.db`），重建丢数据；prompts 加载 404 | `docker-compose.yml` 显式注入容器内绝对路径 `DATABASE_URL=sqlite:////app/data/app.db`、`ATTACHMENT_DIR=/app/data/attachments`（config 对绝对路径原样透传）；新增 `config.prompts_dir()`（基于 app 包目录 + 可选 `PROMPTS_DIR` 环境变量），replier/classifier/retention 统一使用，本地 `backend/docs/prompts` 与容器 `/app/docs/prompts` 均正确 | ✅ |
| 3 | `DEFAULT_SECRETS_FILE` 用 `parents[2]` 落在 `backend/data/`，与 DB 不同根且未被 gitignore | secrets 路径改为 `config.REPO_ROOT / "data" / "secrets.bin"`（`default_secrets_file()`，延迟导入避免循环依赖），与 app.db 同根；`.gitignore` 新增 `backend/data/*`（白名单 `.gitkeep`） | ✅ |
| 4 | 补偿超时 job 单条失败中断整批，审计随回滚丢失 | `_job_retention_timeout_scan` 每条 draft 独立 try/except，失败 rollback 后继续下一条；告警审计与放行审计在对应动作后即时 `db.commit()`（逐条小事务） | ✅ |
| 5 | 自动放行后原补偿草稿仍 pending_review，老板批准会重复发补偿信 | 自动放行成功（release sent）时原草稿置 `superseded`（replies 新状态值，前端显示「已自动放行」）；approve/send 接口拒绝非 pending_review/draft 草稿，并新增 `_has_newer_release` 二次防线：会话已有更新的 sent retention_release 时返回 409 SUPERSEDED | ✅ |

**聚合方案的业务边界（本次实现决策）**：仅「低风险 auto_send」路径聚合；高风险安抚/工单、挽留、审核、人工、静默等保持逐封。同一轮拉取中同一会话若既有高风险又有低风险邮件，高风险照常安抚+建单，低风险仍单独聚合发送一封普通回复（客户会收到安抚信 + 普通回复各一封）。该行为是否符合预期请见文末待确认清单。

## 🔧 Review 修复（2026-08-17，第二批 3 项）

| # | 问题 | 修复 | 状态 |
|---|---|---|---|
| 1（必须修） | M-20 密钥加密在生产 Docker 里不生效：`default_secrets_file()` 无 settings 时按 `REPO_ROOT` 解析，容器内得到 `/data/secrets.bin`（卷外），`encrypt-secrets` 生成的文件运行期找不到，静默回退 .env 明文 | 引入单一数据根 `DATA_DIR`：`Settings.data_dir`（默认 `data`，相对路径按 `REPO_ROOT` 解析，Docker Compose 显式 `DATA_DIR=/app/data`）；`database_url`/`attachment_dir` 默认值由 `data_dir_path` 派生；`default_secrets_file(settings)` 改为 `Path(settings.attachment_dir).parent / "secrets.bin"`（无 settings 时回退仓库根 `data/`）；`cli.encrypt-secrets` 默认路径用 `default_secrets_file(settings)`——本地与容器内 secrets.bin 均与 app.db/附件同目录 | ✅ |
| 2 | `translator.py` 提示词路径仍用 `parents[2]` 猜测，与 classifier/replier/retention 的 `prompts_dir()` 不一致 | 统一改用 `prompts_dir() / "translate_reply.md"`，行为不变 | ✅ |
| 3 | 聚合生成失败 `_remove_ingested_batch` 只删邮件，残留「本批新建的空会话」与指向已删邮件的 `classified` 审计（悬空引用） | 删除邮件后：同步删除本批 `classified` 审计；若会话由本批新建且删除后无任何邮件（历史会话不动），一并删除会话；下轮 poll 干净重建。`ProcessingResult` 新增 `conversation_created` 标记（来自 merge 结果） | ✅ |

## ✅ 已完成项（对照 TECH.md 模块清单）

| 模块 | 内容 | 状态 |
|---|---|---|
| M-18 | 告警通道：`services/alerting.py`，SMTP 邮件（复用 MailerService.send_text，3 次重试、不计入对客限频）+ Bark Webhook（POST JSON，裸 key 自动补 `https://api.day.app/`）；任一通道未配置则静默跳过并写日志，发送失败只记日志不影响主链路；告警内容含 UTC 时间与错误摘要 | ✅ |
| M-18 | LLM 连续失败升级：`llm/client.py::chat_with_retry` 失败路径接入进程内计数器（5 分钟内连续 ≥5 次 → Bark+邮件双通道，成功即清零） | ✅ |
| M-18 | IMAP 拉取连续 3 个轮询周期失败 → 告警（`IngestService.fetch_and_process` 异常路径计数，成功清零）；PRD 异常场景 1 的「重试 3 次 + 邮件积压不丢 + 恢复后批量拉取」由既有 `_connect` 3 次重试 + UNSEEN 保留保证 | ✅ |
| M-17 | 审计全动作覆盖：补 `duplicate_skipped`、`silenced_set`、`high_risk_followup`、调度侧 `auto_close` / `sla_overdue` / `retention_timeout_alert` / `retention_auto_released`；原有 classified/auto_sent/reassured/review/manual/silenced/paused/failed/retention_*/ticket_created/kb_*/qa_*/登录登出等已核对无缺 | ✅ |
| M-17 | `GET /api/v1/audit-logs`（require_owner，Query: action / actor_id / from / to / page / size，按 at 倒序）；前端审计日志页（筛选 + 分页，中文界面），挂到导航与设置页 | ✅ |
| M-12 | `services/scheduler.py`：BackgroundScheduler 5 job —— ① 每 90s（POLL_INTERVAL_SECONDS）IngestService 拉邮件（内部仍同步串行）② 每小时会话自动关闭（last_activity_at < now-30d 且 status!=resolved → resolved + 审计 auto_close）③ 每 30 分钟 SLA 逾期工单告警（pending/in_progress 且 sla_deadline<now）④ 每 30 分钟补偿挽留待审核超时（>24h → 告警老板；仍无处理 → 生成 retention_release 并 SMTP 发送，同 retention.py release 路径）⑤ 每 30s 心跳 | ✅ |
| M-12 | FastAPI lifespan 启动/关闭调度器；`cli.py` 的 poll/run 保留为手动/调试入口，生产以 scheduler 为准 | ✅ |
| M-12 | `GET /api/v1/healthz` 增强：`{db, scheduler, uptime_sec}`，DB 或 scheduler 心跳超 60s（N-4）时返回 503，供 Docker healthcheck | ✅ |
| M-20 | `core/security.py`：Fernet 加解密 + `data/secrets.bin` 读写（0600 权限）；`config.py` 运行期 `get_settings()` 检测到 secrets.bin 存在则解密覆盖 SECRET_FIELDS（EMAIL_PASSWORD / DEEPSEEK_API_KEY / OPENAI_API_KEY / SECRET_KEY / AGENT_SERVICE_TOKEN），文件缺失/密钥为空/解密失败均回退 .env；`python -m app.cli encrypt-secrets [--file]` 不删除 .env 明文。2026-08-17 补：secrets 路径经 `DATA_DIR` 单一数据根派生，容器内为 `/app/data/secrets.bin`（卷内），本地为仓库根 `data/secrets.bin`，与 app.db 同目录 | ✅ |
| F-09 | 前端设置页：暂停/恢复开关（复用 /system/pause|resume）、通知设置只读展示（新增 `GET /api/v1/system/notifications`，只返回是否配置 + 脱敏邮箱）、审计日志入口；导航挂「设置」「审计日志」（收件箱/工单/知识库/标准问答/设置/审计日志） | ✅ |
| 行为补缺 | PRD 异常场景 6（空/纯图片正文 → unknown 转人工 + 标记可疑，不回自动回复）、8（同会话高风险追问合并进原工单，不重复发安抚信/不重复建单，审计 high_risk_followup）、9（客户要求不再联系 → silenced_until=now+72h，审计 silenced_set）、15（同 display_name 不同邮箱 → conversation 详情返回 suggested_merge_conversation_id 供老板决策） | ✅ |
| E2E | PRD 异常场景 1-22 全覆盖（tests/test_e2e_exceptions.py，25 个用例），复用 FakeSMTP/FakeIMAP/MockLLM + httpx ASGITransport，无浏览器/真实网络 | ✅ |
| 部署 | backend/Dockerfile（Python 3.11 slim + uvicorn，非 root appuser，data/ 卷）、frontend/Dockerfile（Node 构建 → Nginx）、docker-compose.yml（backend+frontend+appdata 卷，restart:always，healthcheck 用 /api/v1/healthz，certbot 可选 profile）、nginx.conf（HTTP 开箱即用，同域反代无 CORS）+ nginx-https.conf.example（Let's Encrypt 模板） | ✅ |
| 依赖 | pyproject 新增 `apscheduler>=3.10,<4`、`cryptography>=42.0`（已安装 apscheduler 3.11.3 / cryptography 50.0.0） | ✅ |
| 文档 | README 重写：本地启动、ENCRYPTION_KEY 生成 + encrypt-secrets、.env 新配置、healthcheck、本地 Compose 与生产 HTTPS 两套部署步骤 | ✅ |

## ⚠️ 未决项 / 已知风险

1. **告警去重为进程内存态**：SLA 逾期与补偿挽留超时告警用进程内 set 去重（每个工单/草稿每进程告警一次），重启后重新计数；补偿挽留自动放行用 DB 查询去重（会话已有更新的 sent retention_release 则跳过），重启不重发。可接受（单进程部署）。
2. **IMAP 失败计数语义**：`fetch_and_process` 整体异常（连接/登录/SEARCH/FETCH）计为一次失败轮次；单封邮件处理失败由 `process_one` 内部兜底，不计入 IMAP 告警。
3. **LLM 失败告警窗口**：5 分钟滑动窗口内连续失败 ≥5 次触发；成功调用清零。告警本身不发审计（避免刷屏），Bark/邮件通道失败仅日志。
4. **调度 job 与 SQLite 并发**：APScheduler 多 job 各自开 session，WAL + busy_timeout=5000 兜底；邮件拉取 job `max_instances=1, coalesce=True` 防止叠加。
5. **healthz 依赖 lifespan**：未通过 lifespan 启动 scheduler 时（如测试环境）healthz 返回 503；Docker 部署经 lifespan 正常启动。
6. **补偿挽留超时自动放行依赖 LLM**：release 回复走 replier 生成，若 LLM 不可用则该条 draft 失败记日志并跳过，不影响同批其它 draft；告警与放行审计在动作后即时提交不丢失；下一轮扫描会重试失败的 draft（2026-08-17 已修复 review #4）。
7. **encrypt-secrets 后仍需人工清理 .env**：按 M-20 设计保留明文回退，生产清理需老板手动执行（README 已说明）。2026-08-17 已修复容器内 secrets 路径（DATA_DIR 单一数据根，`/app/data/secrets.bin` 在卷内）。
8. **前端未做 Playwright/浏览器 E2E**：按红线保持轻量，前端交互由 tsc 构建 + 手工验证（`npm run build` 已通过）。
9. **Phase 3 遗留风险继续有效**：知识库全文注入可能超上下文（建议精炼 FAQ）；QA 关键词预命中极端表述可能漏；登录防爆破/登出吊销为进程内存态；真实 .env 凭据未提交。

## ❓ 待老板确认 / 提供（一次性集中列出）

**Phase 3 遗留（不阻塞，继续待确认）**
1. 知识库文档规模：全文注入红线下，是否接受「知识库只放精炼 FAQ/政策文档，不放长篇手册」的使用约定？
2. QA 命中判定：当前关键词预命中 + LLM 语义兜底，是否接受？
3. 标准 QA 上限 100 条：超过时只注入前 100 条，是否接受？
4. 未确认信息标注文案：`"Please note: some information is not confirmed and requires manual verification."` 是否接受？

**Phase 4 新增（阻塞上线，需老板提供真实值）**
5. `ALERT_BARK_WEBHOOK`：Bark Webhook URL（或裸 device key），填入 .env。
6. `ALERT_EMAIL_TO`：告警接收邮箱。
7. `ENCRYPTION_KEY`：老板用 `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` 生成后填入 .env，再执行 `python -m app.cli encrypt-secrets`。
8. 生产域名 + DNS：Nginx HTTPS 模板与 certbot 签发需要真实域名（模板内 `YOUR_DOMAIN` 替换）。
9. 生产部署确认：Hostinger VPS 是否已安装 Docker + Compose，80/443 端口是否放行。

**2026-08-17 Review 新增（聚合业务歧义，不阻塞代码）**
10. 会话聚合只覆盖「低风险 auto_send」：同一会话一轮内同时有高风险 + 低风险邮件时，当前实现为高风险照常安抚+建单、低风险仍单独聚合发送一封普通回复（客户共收到 2 封）。是否接受，还是高风险会话内的低风险邮件也应一并转为人工/合并处理？
11. 聚合失败语义：SMTP 失败保留 failed 草稿下轮重发同一内容；LLM 生成失败删除本批已入库邮件、下轮完整重试。是否接受该失败策略？

## 阶段声明

**Phase 4（里程碑 D）已完成，MVP 完整可上线；未 git commit。**
待老板按上方清单提供真实配置后，即可按 README「6.2 生产部署」上线。

## 测试运行输出（本阶段交付证据）

```text
183 passed in 91.00s (0:01:31)   # 2026-08-17 review 修复后（含聚合/调度/路径/草稿状态回归测试）
189 passed in 91.88s (0:01:31)   # 2026-08-17 第二批收尾（DATA_DIR/secrets 路径、translator、聚合清理）
```

## 🐳 Docker 冒烟验证（2026-08-17，隔离 mock 环境）

隔离冒烟（`/tmp/shouhou-smoke`，独立 `smokeappdata` 卷 + mock env，绝不注入真实 `.env`，验证后已 `down -v` 清理）：

1. **发现并修复构建阻断 bug**：`backend/Dockerfile:20` 原为 `COPY .env.example ./`，但 build context 是 `backend/`，而 `.env.example` 在仓库根 —— 容器内无此文件，`docker-compose build` 必失败。已删除该 COPY（容器 env 由 compose `env_file` 注入，模板留仓库根供部署文档使用）。**单测无法覆盖此路径，只有真实构建能暴露。**
2. **容器启动**：`docker-compose up` 后端起容器成功；`/api/v1/healthz` → `200 {"db":"ok","scheduler":"ok"}`；`/` → `200 {"app":"shouhou-agent","phase":4}`。
3. **数据落卷**：`/app/data` 下 `app.db`（WAL 模式 app.db-shm/-wal）+ `attachments/` + `exports/` 全部在 `appdata` 卷内。
4. **M-20 容器内闭环**：`default_secrets_file(get_settings())` → `/app/data/secrets.bin`（卷内）；容器内跑 `python -m app.cli encrypt-secrets` 后 `secrets.bin` 落卷，权限 `0600 appuser`（非 root），运行期 `get_settings()` 解密回环成功。**密钥加密在生产 Docker 路径确认生效。**

结论：Phase 4 Docker 部署路径冒烟通过；剩余待办仅为「待老板确认/提供」的真实配置清单（见上文 ❓ 章节）与上线动作。
