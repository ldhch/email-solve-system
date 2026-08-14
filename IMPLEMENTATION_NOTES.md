# IMPLEMENTATION_NOTES — Phase 1（里程碑 A）

> 记录日期：2026-08-14
> 范围：TECH.md 第九章 Phase 1，未实现 Phase 2+ 内容

## ✅ 已完成项（对照 TECH.md 模块清单）

| 模块 | 内容 | 状态 |
|---|---|---|
| M-01 | FastAPI 入口 `main.py`，挂载 system 路由，async 异常处理器（M-21 要求） | ✅ |
| M-02 | `config.py`：pydantic-settings 从 `.env` 加载全部配置；相对 SQLite 路径锚定仓库根 | ✅ |
| M-03 | `db/`：SQLite WAL + busy_timeout，`create_all` + seed（`system_state` 单行）；无迁移框架 | ✅ |
| M-04 | `services/ingest.py`：IMAP UNSEEN 拉取（3 次登录重试）、RFC822 解析、Message-ID 去重、2MB 正文截断、5MB 告警日志、附件落盘、bleach 清洗 HTML | ✅ |
| M-05 | `services/conversation.py`：In-Reply-To/References 优先 → 主题精确/`difflib≥0.85` 相似度兜底 + 7 天窗口；会话风险取最高；窗口滑动 | ✅ |
| M-06 | `services/classifier.py`：LLM 输出 risk_level/confidence/category/chargeback_risk/summary_cn；拒付关键词+LLM 双通道；低置信度降级 `unknown` 转人工；`RISK_ACTIONS` 硬编码映射 | ✅ |
| M-07 | `services/replier.py`：聚合会话最近 6 条记录注入 prompt，禁止编造事实；生成→建 Reply→SMTP→sent/failed | ✅（Phase 1 仅注入会话历史，知识库/QA 属 Phase 3） |
| M-09 | `llm/client.py`：DeepSeek（OpenAI 兼容端点）封装，重试 + token 统计；`LLM_PROVIDER` 可切 openai/mock；新增 provider 只需加一个类 | ✅ |
| M-11 | `services/mailer.py`：SMTP_SSL 发送、3 次重试、线程保持（In-Reply-To/References）、可选每小时限流 | ✅ |
| M-17 | `services/audit.py`：最小版审计（classified/reply_sent/reply_failed/requires_manual/paused_skipped/silenced_skipped/pause/resume/pipeline_failed） | ✅ |
| M-19 | `api/system.py`：`GET /status`、`POST /pause`、`POST /resume`、`GET /healthz`；暂停态由 `system_state.ai_paused` 控制，管道处理前检查 | ✅ |
| — | `cli.py`：init-db / poll / run / status / pause / resume / simulate（离线演示） | ✅ |
| — | 测试：45 个 pytest 用例全部通过（见文末输出） | ✅ |
| — | README.md：本地启动、.env 说明、验证方法 | ✅ |

## ⚠️ 未决项 / 已知风险

1. **本机沙箱限制导致的三处工程决策**（正常服务器无影响）：
   - 本环境 seccomp 使 anyio 线程池无法执行任务（同一代码在沙箱外验证正常）。为避免 `pytest` 在本环境挂起，FastAPI 端点与依赖统一写成 `async`，并在 `main.py` 注册 async 异常处理器。
   - `starlette.testclient`（依赖 httpx2 portal）在本环境不可用，API 测试改用 `httpx.ASGITransport` + 单事件循环 `asyncio.Runner`。
   - 依赖锁定：`fastapi==0.115.12` / `starlette 0.46.x` / `anyio==4.9.0`（新版 TestClient 有挂起问题，与沙箱无关的正常环境也可能受影响）。
2. **`deepseek-v4-flash` 模型名未实测**：TECH.md 指定该模型名，但 DeepSeek 账户实际可用模型 ID 需在控制台确认；若调用报 404/模型不存在，改 `LLM_MODEL` 即可（Phase 1 不写死）。
3. **Titan IMAP/SMTP 地址与端口未实测**：`.env.example` 使用官方常见值 `imap.titan.email` / `smtp.titan.email:465`，需按 Titan 后台实际显示核对。
4. **非低风险邮件的 Phase 1 兜底**：high/medium/unknown 邮件完整落库并写审计，但**不自动回复、不建工单、不发安抚信**（工单/安抚属 Phase 3，审核队列属 Phase 2）。这是「宁可保守不自动发」的刻意取舍，等待后续阶段补齐。
5. **退款/退货/换货请求在 Phase 1 一律不自动回复**（挽留策略属 Phase 2），分类为 `refund_request` 即转人工。
6. **调度**：Phase 1 用 `cli run` 简单轮询循环；APScheduler（M-12）留到 Phase 4。
7. **`users` 表与 `audit_logs.actor_id` 外键**：Phase 1 未建 users 表，`actor_id` 为无外键的可空整数（NULL=AI 管道）；Phase 2 建表后补外键。
8. **SMTP 限流默认关闭**（`SMTP_RATE_LIMIT_PER_HOUR=0`）；R5 建议生产设 6。当前超限时回复标记 failed 并留待人工/下轮处理，未做自动重排期。
9. **暂停期间**：邮件只拉取不处理、保持 IMAP 未读，恢复后由轮询自动补处理（符合 PRD F9 / TECH 5.6 积压处理语义）。
10. **审计为最小版**：未做后台查询页、CSV 导出、防篡改（均属 Phase 4）。
11. **healthz 不含 scheduler 心跳**：Phase 1 无 scheduler，返回 `db/uptime_sec`；Phase 4 按 N-4 补齐。

## ❓ 待老板确认的问题（一次性集中列出）

1. **DeepSeek 模型 ID**：你的 DeepSeek 账户中实际可用模型是否就是 `deepseek-v4-flash`？（若不是，请告知正确模型名，我改 `.env.example` 默认值；否则线上首次调用会失败）
2. **Titan 邮箱参数**：请在 Titan 后台确认 IMAP 地址/端口、SMTP 地址/端口，以及是否支持应用专用密码；本阶段按 `imap.titan.email:993` / `smtp.titan.email:465` 预设。
3. **Phase 1 非低风险邮件暂不自动回复**：高风险（含拒付）与中风险邮件本阶段只入库+审计、不发信，等 Phase 2/3 补审核与安抚工单——是否接受此过渡行为？
4. **暂停接口的令牌**：Phase 1 用 `AGENT_SERVICE_TOKEN` 保护暂停/恢复接口（老板先通过 CLI 或 curl 使用）；Phase 2 换成登录后自动失效——是否接受？
5. **SMTP 发送频率**：生产是否按 R5 建议开启每小时 6 封限流？
6. **回复质量确认**：Phase 1 回复只注入会话历史，无知识库/QA（Phase 3 才有）；低风险咨询类回复是否满足预期？

## 测试运行输出（本阶段交付证据）

```text
============================= test session starts ==============================
platform linux -- Python 3.13.11, pytest-9.1.1, pluggy-1.6.0
rootdir: /home/work-first/project/other-agent/kefu/shouhou-agent/backend
configfile: pyproject.toml
collected 45 items

tests/test_api_system.py ......                                          [ 13%]
tests/test_classifier.py ......                                          [ 26%]
tests/test_config.py ....                                                [ 35%]
tests/test_conversation.py ..........                                    [ 57%]
tests/test_ingest.py .......                                             [ 73%]
tests/test_mailer.py ......                                              [ 86%]
tests/test_pipeline.py ......                                            [100%]

============================== 45 passed in 9.97s ==============================
```

## 阶段声明

**Phase 1（里程碑 A）已完成；未实现 Phase 2 及以后的内容。** 请确认「待老板确认的问题」后，再进入 Phase 2。
