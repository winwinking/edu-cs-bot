# 要求对照清单

对应 `docs/PHASE5.md` 5.5。按 `docs/REQUIREMENTS.md` 顺序逐条对照，每条写完成情况（完成 /
部分完成 / 未做）、在哪、怎么验证。部分完成和未做的链接到 `docs/KNOWN_ISSUES.md` 对应条目。

## 三、功能需求

### FR-1 IM 接入与会话 —— 完成

- WebSocket 接入：`app/gateway/main.py` `/ws`。验证：`tests/integration/test_im_inbound_and_queue.py`。
- 消息去重、幂等：Redis 去重键 + DB 唯一约束 + `messages.status`（`app/gateway/message_handler.py`、
  `app/worker/handler.py`）。验证：`tests/unit/test_gateway_rate_limit_dedup.py`、
  `tests/unit/test_worker_handler_idempotency.py`、`tests/e2e/test_e2e_09_duplicate_message_id.py`。
- ACK：`AckMessage`（`app/common/schemas.py:28-36`）。
- 断线重连、多端同步：`app/gateway/connection_manager.py`（按 `(tenant_id,user_id)` 分组转发，
  Redis 断线指数退避重连）。验证：`tests/unit/test_connection_manager.py`；阶段一收尾冒烟
  测试实测复现过并发重连场景（`AGENT_LOG.md` 索引第 6、17 条）。
- 上下文：短期记忆（最近 10 条原文）+ 滚动摘要：`app/worker/handler.py:192-198`、
  `app/worker/graph/context_summary.py`。验证：`tests/unit/test_context_summary.py`。
- 多租户隔离：所有业务表带 `tenant_id`，所有查询带过滤条件（见 NFR-3）。
- 流式回复：`reply_chunk` 按句子切分发送，`app/worker/graph/graph.py:226-247`。

### FR-2 意图识别与路由 —— 完成

- 7 类意图（平台指令/知识问答/日程提醒/财务查询/闲聊/转人工/高风险敏感操作）：
  `app/worker/graph/classify.py`、路由表 `app/worker/graph/graph.py:41-53`。
- 规则+LLM 混合：`app/worker/graph/classify.py:266-324` `_classify_core()`。
- 工具调用 JSON Schema 校验：`app/common/tools.py:241-259` `parse_tool_call()`。验证：
  `tests/unit/test_tool_guard.py`、`tests/unit/test_reminder_tool_args.py`。
- LLM 输出不直接执行：三层校验见 `docs/ARCHITECTURE.md` 第 6 条。验证：
  `tests/e2e/test_e2e_08_llm_invalid_json_fallback.py`。

### FR-3 平台客户端指令运行 —— 完成

- 关闭/开启自动续费、修改课程提醒、打开课程表、提交请假、查询学习报告、转接人工：
  `app/worker/graph/command.py`、`app/worker/graph/handoff.py`。
- 执行前权限校验：身份只认 JWT+DB（`app/common/permissions.py`）。
- 高风险指令二次确认：`app/worker/graph/command.py:237-350`（生成待确认）、`:353-411`
  （原子抢占确认）。验证：`tests/e2e/test_e2e_04_disable_auto_renew_confirmation.py`。
- 下发 mock-platform，支持超时/重试/幂等：`app/common/platform_client.py`、
  `mocks/mock_platform/main.py:150-182`（幂等键 + 锁）。验证：
  `tests/unit/test_platform_confirmation.py`、`docs/FAULT_INJECTION.md` 故障 5/6。
- 结果转自然语言：`_LOW_RISK_REPLY`/`_build_success_reply()` 等模板（`command.py`）。

### FR-4 知识问答 —— 部分完成

- RAG 检索：`app/worker/graph/knowledge.py`，检索器 `app/common/retrieval.py`（pgvector 或
  mock_knowledge）。
- 给出依据来源：出处由代码拼接（`_build_lead_in()`），不是 LLM 生成。
- 无依据不编造：`KNOWLEDGE_NO_HIT_REPLY` 固定话术（`knowledge.py:95-100`）。验证：
  `tests/e2e/test_e2e_10_knowledge_no_hit.py`。
- 知识库更新后重新索引：`scripts/reindex.py`（按内容哈希增量重建）。
- 多轮澄清：**部分完成**。当前实现是"追问改写"（"那寒假班呢？"这类追问，把上一句拼进查询
  提高权重，`knowledge.py:35-46`），系统不会主动向用户提问"你是指哪个班型？"来澄清；主动
  提问式澄清目前只在平台指令（多门课匹配时问"要开通哪一门"）和提醒（多条提醒匹配时问"要操作
  哪一条"）两个节点实现，知识问答节点没有这种主动澄清能力。评测集 `kq05`/`kq06` 覆盖的是
  "追问改写"场景，不是"主动澄清"场景。见 `docs/KNOWN_ISSUES.md` 第 7 条（检索排序问题
  是这类追问失败的主要原因，`docs/EVAL_REPORT.md` `kq06`/`kq07`/`kq09`/`kq10`）。

### FR-5 日程安排和提醒 —— 完成

- 创建、修改、取消、查看：`app/worker/graph/reminder.py`。验证：
  `tests/e2e/test_e2e_05_reminder_push.py`、`scripts/phase3_smoke.py`。
- 时区 Asia/Shanghai：`reminders.timezone` 存机构时区名，默认取 `tenants.timezone`
  （`app/common/reminder_rules.py:41-45`）。
- 重复规则（每天/每周/工作日）：`app/common/reminder_rules.py:86-96`。不含法定节假日调休，
  见 `docs/KNOWN_ISSUES.md` 第 21 条（题目原文只要求"如每天、每周、工作日"，字面要求已满足，
  这条是额外发现的局限）。
- 到点推送：`app/scheduler/loop.py`，`FOR UPDATE SKIP LOCKED` 每秒扫描。
- 提前提醒：`advance_minutes` 字段，默认 30 分钟（`reminder.py:38`）。
- 重启不丢：状态全部落 PostgreSQL，`docker-compose.yml` 持久化数据卷；先推送再提交的代价
  见 `docs/ARCHITECTURE.md` 第 11 条。

### FR-6 财务数据调取和回复 —— 完成

- 订单/账单/发票/退费/余额：`app/worker/graph/finance.py`，mock-finance 五个接口。
- tenant_id/user_id/权限校验：`app/common/permissions.py:23-31` + mock-finance 自己的第二层
  （`mocks/mock_finance/main.py:228-243`）。验证：
  `tests/e2e/test_e2e_03_finance_cross_user_forbidden.py`、`scripts/finance_probe.py`。
- 敏感信息脱敏：`app/common/masking.py`（邮箱/手机号/身份证/银行卡）。验证：
  `tests/unit/test_masking.py`、`tests/e2e/test_e2e_02_finance_invoice_masking.py`。
- 超时/失败不编造：`FinanceUnavailable` → 固定话术 + `FollowupTask` 留痕（`finance.py:230-251`）。
  验证：`tests/e2e/test_e2e_07_finance_timeout_no_fabrication.py`。
- 全部写审计日志：`_write_audit_log()`（`finance.py:112-143`），成功/拒绝/上游错误都记。

### FR-7 人工转接 —— 完成

- 触发："转人工"关键词（`HANDOFF_KEYWORDS`，`classify.py:36`）、连续两次不满意
  （`DISSATISFIED_KEYWORDS`，`classify.py:42`，计数逻辑在`:294-305`）、LLM 判断（function
  calling 选中 `transfer_to_human` 工具）。
- 携带摘要/意图/已尝试操作/风险提示：`app/worker/graph/handoff.py:220-286`。验证：
  `tests/e2e/test_e2e_06_handoff_with_summary.py`。
- 坐席不在线给出留言方案：`_build_offline_reply()`（`handoff.py:117-121`），服务时间读
  `tenants.service_hours`。

### FR-8 语言风格 —— 完成

- 少 AI 味规则打分：`eval/SCORING.md`，禁用词/emoji/重复道歉/具体信息/不确定性五项。
- 评测结果：平均分 95.8，80 分以上占 86.0%（43/50），见 `docs/EVAL_REPORT.md`。7 道未过 80
  分的题全部是模板回复"缺具体信息/下一步指引"或"该说不确定却没说"（`cmd01/03/04/05`、
  `rem03`、`nohit04/05`），不是出现禁用词——这是评测发现的可改进点，不是评测未覆盖，已记入
  `docs/KNOWN_ISSUES.md`（第 5.4 评测相关条目）。
- 高风险操作二次确认：见 FR-3。
- 不确定时明确说不确定：`KNOWLEDGE_NO_HIT_REPLY`、`FINANCE_UPSTREAM_ERROR_REPLY` 等固定话术
  都含"暂时没有查到/查不到"这类明确表述。

## 四、非功能需求

### NFR-1 高并发 —— 部分完成

- 架构层面（水平扩展、异步解耦、限流、熔断、降级、重试、死信）全部完成：`docker-compose.yml`
  `--scale worker=N`、`app/common/mq.py`、`app/common/rate_limit.py`、
  `app/common/circuit_breaker.py`、`app/worker/consumer.py`。
- 压测指标跟题目逐条对比（`docs/LOADTEST.md`"和题目指标逐条对比"一节）：
  - 稳定 500 连接/200msg/s/5 分钟：**不达标**，1 worker 约 10 msg/s、3 worker 约 37 msg/s，
    worker 单核 CPU 是瓶颈。
  - 突发 1000 msg/s/30 秒：**部分达标**，并发数和发送速率达标、零丢失，但积压要 12~13
    分钟才能消化完（题目没有明确"多久消化完"的指标，只要求"不丢消息，恢复后能处理"，这点
    满足）。
  - 财务查询 100 QPS/P95<500ms：**不达标**，`mock-finance` 本身 P95 只要 286ms，瓶颈是排队
    等 worker。
  - LLM 超时率 20% 降级且不崩溃：**达标**。
  - 入站 ACK P95<300ms、首响 P95<1.5s、完整回复 P95<3s、错误率<1%：具体数字见
    `docs/LOADTEST.md`，稳定场景吞吐不达标的情况下这几个延迟指标也相应受影响。
  见 `docs/KNOWN_ISSUES.md` 第 10 条（CPU 瓶颈未做逐函数 profiling）和后续规划第 3 条
  （Postgres 扩容/查询优化验证未做）。

### NFR-2 可靠性 —— 完成

- 至少一次投递+业务幂等：RabbitMQ `delivery_mode=PERSISTENT` + publisher confirm + 两层去重。
- 财务/指令/提醒操作可追踪：`audit_logs`/`pending_actions`/`reminders` 表 + `trace_id`。
- 服务重启提醒不丢：见 FR-5。
- LLM 超时/finance 500/Redis 故障降级：熔断器 + `app/common/redis.py` fail-open。验证：
  `docs/FAULT_INJECTION.md` 故障 1、2、9、13、15。

### NFR-3 安全 —— 完成

- 多租户隔离：所有业务查询带 `tenant_id` 过滤（`app/worker/graph/*.py` 各节点、
  `app/common/permissions.py`）。
- JWT 鉴权：`app/common/auth.py`，gateway 验签、mock-im 演示专用签发。
- RBAC：`app/common/models.py` `UserRole`（student/parent/agent/admin），
  `app/common/permissions.py` 按角色限定财务查询范围。
- 敏感字段脱敏：见 FR-6。
- 防 Prompt Injection：用户输入只放 user 消息（`app/worker/graph/knowledge.py:122-127`
  资料块同样放 user 消息，不放 system prompt），加上 `app/common/prompt_guard.py`
  `detect_prompt_injection()` 打标记供审计/转人工参考——真正的防线是"权限校验和工具白名单
  不看消息内容里说了什么"，检测标记本身只是辅助信号，不是唯一防线。验证：
  `tests/integration/test_llm_mock_tool_call.py`、评测 `inj01`~`inj03`。
- 防 SQL 注入：全部走 SQLAlchemy 表达式/绑定参数，`scripts/sql.py` 只读查询工具也拒绝写操作。
- 防越权查询：见 FR-6、`tests/e2e/test_e2e_03_finance_cross_user_forbidden.py`。
- 日志不记录完整身份证/银行卡/密码/token：`app/common/logging.py` 两层脱敏（见
  `docs/ARCHITECTURE.md` 第 18 条）。
- 密钥不提交仓库：`.gitignore` 排除 `.env`，`git log` 确认从未提交过 `.env`；
  `git grep -n "eyJ"` 应为空（AGENT_LOG 索引第 19 条修复过一次真实误提交）。

### NFR-4 可观测 —— 完成

- 结构化日志带 trace_id/tenant_id/conversation_id：`app/common/logging.py`
  `bind_trace_context()`，三个服务全覆盖（scheduler 补齐见 AGENT_LOG 索引"自查修复"）。
- Prometheus 指标：QPS/延迟（`app/gateway/metrics.py`、`app/worker/metrics.py`）、错误率
  （`worker_messages_total`）、队列积压（`app/worker/consumer.py:109-129`
  `run_queue_backlog_poller()`）、LLM token（`app/common/llm_usage.py:30`
  `llm_tokens_total`）、工具成功率（`tool_calls_total{tool,status}`，可用 `status=ok` 比例
  算出）。
- 关键链路追踪：靠手工传递的 `trace_id` 串联各服务日志，不是标准 OpenTelemetry 协议，见
  `docs/KNOWN_ISSUES.md` 第 20 条（未做的是可视化调用链工具，`trace_id` 本身的可追踪性已
  满足）。另有两处小缺口：`redis_unavailable` 告警不带 `trace_id`（第 11 条）、worker 日志被
  `/health` 探针淹没（第 12 条）。
- 审计日志：`audit_logs` 表记录所有财务查询和平台指令执行。

### NFR-5 成本与评测 —— 完成

- token 用量按租户统计：`llm_usage` 表 + `worker_llm_tokens_total{tenant_id,direction}`。
- 模型路由或降级：只做了降级（LLM 不可用时切换关键词规则/固定模板），没做按问题复杂度路由
  到不同模型，题目原文"或"字表明二者选一即满足；多模型智能路由未做见
  `docs/KNOWN_ISSUES.md` 第 22 条。
- 离线评测集：`eval/cases.jsonl`（50 条）+ `docs/EVAL_REPORT.md`（事实准确率、引用命中率、
  越权拒绝率、少 AI 味评分、转人工准确率）。

## 五、运行环境 —— 完成

- Docker Compose 一键启动：`make up` 起基础设施 + 应用服务 + 5 个 mock，自动跑迁移+种子+
  建索引。
- PostgreSQL 15：`docker-compose.yml:158` `pgvector/pgvector:pg15` 镜像（同时满足向量库要求）。
- Redis 7：`docker-compose.yml:175` `redis:7-alpine`。
- 消息队列：RabbitMQ（`docker-compose.yml:190`）。
- 向量库：pgvector，跟业务数据同库（见 `docs/ARCHITECTURE.md` 第 7 条）。
- 5 个 mock 服务：mock-im/mock-llm/mock-knowledge/mock-platform/mock-finance 全部实现。
- `make up`/`make test`/`make loadtest`/`make demo`：全部提供，见 `README.md` Makefile
  目标表。
- `.env.example`：存在，部分项留空（`REDIS_PASSWORD`/`DEFAULT_DAILY_TOKEN_BUDGET`），实测
  原样复制成 `.env` 能正常启动，见 `docs/KNOWN_ISSUES.md` 第 3 条。
- 数据库迁移脚本：`migrations/versions/`，Alembic。
- 健康检查接口：gateway/worker/scheduler 均有 `/health`，5 个 mock 服务均有 `/health`。

## 六、测试要求

### 6.1 单元测试 —— 完成

- 核心模块覆盖率：`Makefile:50-58` 对六个模块（意图路由 `classify.py`、权限校验
  `permissions.py`、幂等 `message_handler.py`/`handler.py`、脱敏 `masking.py`、日程规则
  `reminder_rules.py`、工具参数校验 `tools.py`）统计覆盖率，README 记录的最近一次结果是
  92%，≥70% 达标。这六个模块正好对应题目点名的六类覆盖对象。
- `tests/unit/` 下 34 个测试文件（不含 `__init__.py`），其中 `test_mock_llm_rules.py`/
  `test_mock_llm_timeout.py` 两个额外在 `mocks-tools` 镜像里单独跑一次（针对 mock-llm 自身
  规则），`test_eval_scoring.py` 由 `make eval` 跑（评测打分函数单测），其余 32 个随
  `make test` 的 `tools` 镜像那一层跑。

### 6.2 集成测试 —— 完成

- 用 Docker Compose 启动依赖：`tests/integration/` 五个文件，覆盖 IM 入站+队列
  （`test_im_inbound_and_queue.py`）、LLM mock 工具调用（`test_llm_mock_tool_call.py`）、
  知识检索租户隔离（`test_knowledge_retrieval_tenant_isolation.py`）、财务鉴权
  （`test_finance_mock_auth.py`）、提醒调度推送（`test_reminder_scheduler_push.py`）。

### 6.3 端到端测试 —— 完成

题目点名的 10 个场景与 `tests/e2e/` 文件一一对应：

1. 课程政策引用 → `test_e2e_01_knowledge_policy.py`
2. 发票查询脱敏 → `test_e2e_02_finance_invoice_masking.py`
3. 跨用户财务越权 403 → `test_e2e_03_finance_cross_user_forbidden.py`
4. 关闭自动续费二次确认 → `test_e2e_04_disable_auto_renew_confirmation.py`
5. 提醒 5 秒内推送 → `test_e2e_05_reminder_push.py`
6. 转人工携带摘要 → `test_e2e_06_handoff_with_summary.py`
7. 财务系统超时不编造 → `test_e2e_07_finance_timeout_no_fabrication.py`
8. LLM 非法 JSON 兜底 → `test_e2e_08_llm_invalid_json_fallback.py`
9. 重复 message_id 只处理一次 → `test_e2e_09_duplicate_message_id.py`
10. 知识库无命中不瞎编 → `test_e2e_10_knowledge_no_hit.py`

### 6.4 压测要求 —— 部分完成

- k6 脚本：`loadtest/steady.js`/`burst.js`/`finance.js`/`llm_timeout.js`。
- 压测报告：`docs/LOADTEST.md`，含 QPS/P50/P95/P99/错误率/队列积压/CPU/内存。
- 四个场景全部跑过，具体是否达标见上面 NFR-1（稳定/财务两个场景不达标，是压测**结果**的
  真实发现，不是"没做压测"）。

### 6.5 故障注入 —— 部分完成

- `docs/FAULT_INJECTION.md` 列出全部 19 种注入/恢复命令。
- 题目点名的 8 种亲手验证过（mock-llm 延迟 5 秒/500/幻觉、mock-finance 超时/500、Redis 重启、
  队列积压，另加熔断/降级/重试/死信/告警的验证）。
- 另 3 种由 E2E 测试自动覆盖（LLM 非法 JSON、重复消息、知识库无命中）。
- 其余 8 种只写了命令，没有亲手跑一遍看现场反应，见 `docs/KNOWN_ISSUES.md` 第 8 条。

### 6.6 LLM 质量评测 —— 完成

- 50 条固定测试集：`eval/cases.jsonl`。
- 五项指标：事实准确率、引用命中率、越权拒绝率、少 AI 味评分、转人工准确率，定义见
  `eval/SCORING.md`，结果见 `docs/EVAL_REPORT.md`。
- 用 mock-llm 跑，测的是系统链路，测不到真实大模型生成质量，见 `docs/KNOWN_ISSUES.md`
  第 17 条（真实 LLM 评测未做）。

## 七、负面清单

### 安全类 —— 全部避免

- 硬编码 API Key/数据库密码/token：**避免**，`app/common/config.py` 统一 `Settings`，全部
  从环境变量读；`.gitignore` 排除 `.env`。
- 日志打印完整身份证/银行卡/手机号/密码：**避免**，见 NFR-4；曾有真实漏判（trace_id 被
  银行卡正则误伤）被审查发现并修复，见 `AGENT_LOG.md` 索引第 32 条。
- 不做租户隔离：**避免**，见 NFR-3。
- 不做权限校验，允许越权查询财务：**避免**，两层权限校验，见 FR-6。
- 用户输入直接拼进系统提示：**避免**，见 NFR-3 防 Prompt Injection。
- LLM 输出未经校验直接执行工具：**避免**，见 FR-2、`docs/ARCHITECTURE.md` 第 6 条。
- 高风险操作无二次确认：**避免**，见 FR-3。

### 工程类 —— 全部避免

- 只做单机 demo，无队列/无限流/无熔断：**避免**，RabbitMQ + Redis 限流 + 熔断器全部实现。
- 同步阻塞调用 LLM 导致吞吐极低：**避免**，全异步（`AsyncOpenAI`/httpx.AsyncClient/asyncpg），
  CLAUDE.md 硬性规则 4；压测已实测出真实的吞吐瓶颈在 worker 单核 CPU，不是同步阻塞。
- 无限重试导致雪崩：**避免**，所有重试都有次数上限（`LLM_MAX_RETRIES`/
  `FINANCE_MAX_RETRIES`/`PLATFORM_MAX_RETRIES`/`DLQ_MAX_RETRIES`）+ 熔断器。
- 消息重复处理，财务/指令重复执行：**避免**，见 FR-1 幂等 + FR-3 平台指令幂等键。
- 服务重启后提醒丢失：**避免**，见 FR-5。
- 无健康检查/无迁移/无 README：**避免**，见运行环境一节。
- 测试依赖真实外部付费服务：**避免**，`tests/` 全部走 mock 服务或 Docker Compose 起的
  基础设施，真实 LLM 只用于人工演示（题目允许）。

### AI 类 —— 全部避免

- 财务/政策问题无依据编造：**避免**，见 FR-4、FR-6。
- 知识库无命中仍强行回答：**避免**，见 FR-4。
- 机器人语言充满 AI 味：**避免**，见 FR-8（评测显示无一条触发禁用词，7 条低分是"缺具体信息"
  而不是"套话多"）。
- 不记录 token 成本：**避免**，见 NFR-5。
- 不区分意图，所有问题都直接丢给 LLM：**避免**，见 FR-2（规则先行 + LLM 兜底）。

### 提交类 —— 全部避免（本阶段范围内）

- 只交视频/截图不交源码：**不适用**，本仓库即源码交付物。
- 提交仓库包含 .env/密钥/真实用户数据：**避免**，`.gitignore` + 全部测试数据均为编造的
  mock 数据（`AGENT_LOG.md` 开头说明）。
- 使用 coding agent 生成后完全不审查、无法解释代码：**避免**，见 `AGENT_LOG.md` 审查故事
  索引（本阶段前累计 40 条人工审查/agent 做错记录 + 若干条 agent 自查修复）。
- 抄袭开源项目但不说明来源和改动：**不适用**，全部从零实现。
- 无测试/无压测/无评测报告：**避免**，见 6.1~6.6。

## 八、交付物

1. Git 仓库源码 —— **完成**。
2. README（架构/启动/测试/演示说明）—— **完成**，`README.md`。
3. `docker-compose.yml` 与 `.env.example` —— **完成**。
4. 架构图与关键设计说明 —— **完成**，`docs/ARCHITECTURE.md`（本次新增）。
5. API 文档 —— **完成**，`docs/API.md`（本次新增）。
6. 数据库迁移脚本 —— **完成**，`migrations/versions/`。
7. 单元/集成/E2E 测试 —— **完成**，见 6.1~6.3。
8. 压测脚本与压测报告 —— **完成**（脚本+报告齐全），报告本身记录了部分指标不达标，见
   NFR-1、6.4。
9. LLM 质量评测报告 —— **完成**，`docs/EVAL_REPORT.md`。
10. coding agent 使用记录（关键 prompt/生成模块/人工审查修复点/agent 做错部分）—— **完成**，
    `AGENT_LOG.md`（总览部分随本阶段 5.6 步骤补齐）。
11. 10–15 分钟演示视频 —— **未做**（Jo 负责，`docs/PHASE5.md` 关键设计决定 9：演示视频
    讲稿和模拟面试由 Jo 负责，CC 不参与，不属于本清单核对范围）。
12. 已知问题与后续规划 —— **完成**，`docs/KNOWN_ISSUES.md`。
