# API 文档

对应 `docs/PHASE5.md` 5.5。字段、端口全部对着代码核对，示例里的 token 用占位符
`<u_a_1001的token>`。

## 各服务 FastAPI 自动文档

gateway/worker/scheduler/5 个 mock 服务全部是 FastAPI 应用，默认自动文档地址是
`http://localhost:<宿主机端口>/docs`（Swagger UI）。宿主机端口见下表；容器内部统一是
`8000`（worker 内部是 `8001`，scheduler 内部是 `8002`，见 `app/worker/main.py:2,32`、
`app/scheduler/main.py:2,32`）。

| 服务 | 宿主机端口（`.env` 变量） | 容器内部端口 | 自动文档 |
|---|---|---|---|
| gateway | `GATEWAY_HOST_PORT`（默认 8000） | 8000 | `http://localhost:8000/docs` |
| worker（健康检查/指标） | `WORKER_HEALTH_HOST_PORT`（默认 8011，范围 8011-8019） | 8001 | `http://localhost:8011/docs`（只有 `/health`/`/metrics` 两个接口，业务逻辑不走 HTTP） |
| scheduler（健康检查/指标） | `SCHEDULER_HEALTH_HOST_PORT`（默认 8002，范围 8002-8009） | 8002 | `http://localhost:8002/docs` |
| mock-im（演示控制台后端） | `MOCK_IM_HOST_PORT`（默认 8080） | 8000 | `http://localhost:8080/docs` |
| mock-llm | `MOCK_LLM_HOST_PORT`（默认 8100） | 8000 | `http://localhost:8100/docs` |
| mock-knowledge | `MOCK_KNOWLEDGE_HOST_PORT`（默认 8101） | 8000 | `http://localhost:8101/docs` |
| mock-platform | `MOCK_PLATFORM_HOST_PORT`（默认 8102） | 8000 | `http://localhost:8102/docs` |
| mock-finance | `MOCK_FINANCE_HOST_PORT`（默认 8103） | 8000 | `http://localhost:8103/docs` |

端口映射核对自 `docker-compose.yml`（gateway `:95-96`、worker `:116-117`、scheduler
`:144-145`、mock-llm `:213-214`、mock-im `:226-227`、mock-knowledge `:237-238`、
mock-platform `:246-247`、mock-finance `:256-257`）。worker/scheduler 用端口范围
（`START-END:内部端口`）是为了 `--scale` 扩容时每个副本都能各拿到一个宿主机端口，不会
抢同一个端口启动失败。

## WebSocket 协议（gateway，`app/gateway`）

### 连接

```
ws://localhost:8000/ws?token=<u_a_1001的token>
```

`token` 放在 query string（`app/gateway/main.py:68`），不是 header——原生浏览器 `WebSocket`
API 不支持自定义 header。`accept()` 先完成握手、再校验 token：校验失败调用
`close(code=4401)`（`app/gateway/main.py:62-80`）。这是当前唯一使用的自定义关闭码；
`WebSocketDisconnect`（客户端正常断开）走标准关闭流程，没有额外自定义 code。

### 客户端 → gateway：`ClientMessage`（`app/common/schemas.py:11-24`）

```json
{"type": "message", "message_id": "<uuid>", "conversation_id": "<uuid>", "content": "寒假班请假会退课时费吗？"}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `type` | `"message"`（固定） | |
| `message_id` | `str` | 客户端生成，用于去重/幂等；重发同一个 `message_id` 会被判定为重复 |
| `conversation_id` | `str`（UUID） | 会话 id，同一个会话的历史/摘要/待确认操作按这个关联 |
| `content` | `str` | 用户输入，超过 `WS_MESSAGE_MAX_LENGTH`（默认 2000）字符直接校验失败 |

### gateway → 客户端

四种消息类型，`type` 字段区分：

**`AckMessage`**（`app/common/schemas.py:28-36`）——收到消息的确认：

```json
{"type": "ack", "message_id": "<uuid>", "status": "accepted", "trace_id": "<hex>", "detail": null}
```

`status` 三种取值（`app/gateway/message_handler.py`）：
- `accepted`：限流、去重都通过，已成功投递到 RabbitMQ（`:151`）
- `duplicate`：Redis 去重键已存在（`:130-134`）
- `rate_limited`：超过限流阈值（`:112-116`），`detail` 固定是"发得有点快，稍等几秒再发。"

**`ReplyChunkMessage`**（`app/common/schemas.py:39-45`）——流式回复的一个分片，按句子切分：

```json
{"type": "reply_chunk", "reply_to": "<message_id>", "seq": 0, "delta": "依据《课程服务协议》第 4.2 条："}
```

**`ReplyEndMessage`**（`app/common/schemas.py:48-54`）——流式回复结束，带 `meta`：

```json
{"type": "reply_end", "reply_to": "<message_id>", "meta": { ... 见下 }}
```

**`ErrorMessage`**（`app/common/schemas.py:57-63`）——出错：

```json
{"type": "error", "message_id": "<uuid|null>", "code": "invalid_json", "detail": "消息不是合法的 JSON"}
```

`code` 目前的取值（`app/gateway/message_handler.py`）：`invalid_json`（不是合法 JSON，
`:92-97`）、`invalid_message`（Pydantic 校验失败，`:99-107`）、`mq_publish_failed`
（投递到 RabbitMQ 失败，`:136-149`）；`app/worker/handler.py:175` 处理越权会话时另外发一条
`code="forbidden"`（`app/worker/pubsub.py` 的 `publish_error()`，走 Redis 推送到 gateway，
不是 gateway 直接产生的）。

**`ReminderPushMessage`**（`app/common/schemas.py:66-76`）——提醒到点推送，跟对话回复走
同一条 Redis 频道/gateway 转发链路，客户端靠 `type="reminder"` 区分：

```json
{"type": "reminder", "reminder_id": "<uuid>", "title": "交作业", "text": "提醒：今天 18:30 交作业，还有 30 分钟开始。"}
```

### `reply_end.meta` 字段（`app/worker/graph/graph.py:138-176` `_build_meta()`）

以下逐字段核对自 `_build_meta()` 源码，并用一次真实评测（`eval/output/kq01.json`，知识问答
命中场景）的完整 `meta` 交叉验证：

```json
{
  "intent": "knowledge_qa",
  "route_source": "llm",
  "tools": [{"name": "search_knowledge", "status": "ok"}],
  "citations": [
    {"doc_title": "课程服务协议", "clause_no": "4.2", "score": 0.5393},
    {"doc_title": "课程服务协议", "clause_no": "5.2", "score": 0.4509},
    {"doc_title": "课程服务协议", "clause_no": "4.1", "score": 0.395}
  ],
  "pending_action_id": null,
  "handoff_ticket_id": null,
  "guard": {"dropped_sentences": 0, "banned_phrases_removed": 0},
  "risk_flags": [],
  "context": {"history_messages": 0, "has_summary": false},
  "circuit_breaker": [],
  "budget_exceeded": false,
  "worker_id": "433bcf03b147",
  "path": ["load_context", "classify", "knowledge", "respond"],
  "timings": {"load_context": 3.0, "classify": 324.8, "knowledge": 13.5, "respond": 4117.1},
  "llm_ms": 4419.1
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `intent` | `str \| null` | 判出来的意图，取值见 `app/worker/graph/graph.py:41-53` `_INTENT_TO_NODE` 的键，加上 `high_risk`/`confirm_action`/`cancel_action`/`confirm_ambiguous`/`dissatisfied_first` |
| `route_source` | `str` | `rule`（worker 规则命中）/ `llm`（LLM function calling）/ `rule_fallback`（LLM 调用失败降级成关键词规则），见 `app/worker/graph/classify.py` |
| `tools` | `[{"name","status"}]` | 这轮尝试解析/调用的工具；`status` 常见取值 `ok`/`forbidden`/`invalid_json`/`schema_error`/`upstream_error`/`not_found`/`pending_confirmation`/`need_clarification`/`no_op`/`timeout`，具体含义看各业务节点（`knowledge.py`/`finance.py`/`command.py`/`reminder.py`） |
| `citations` | `[{"doc_title","clause_no","score"}]` | 知识问答命中的检索结果；非知识问答场景是空数组 |
| `pending_action_id` | `str \| null` | 这轮涉及的待确认操作 id |
| `handoff_ticket_id` | `str \| null` | 这轮生成的转人工工单 id |
| `guard.dropped_sentences` | `int` | OutputGuard 因出处核对不通过整句丢弃的句子数 |
| `guard.banned_phrases_removed` | `int` | OutputGuard 删掉的禁用套话出现次数 |
| `risk_flags` | `[str]` | `sensitive_request`/`prompt_injection_suspected` 等，见 `app/worker/graph/classify.py:39,335-340` |
| `context.history_messages` | `int` | 本轮带的原文历史条数 |
| `context.has_summary` | `bool` | 本轮有没有带历史摘要 |
| `circuit_breaker` | `[str]` | 本轮因熔断打开被跳过、没真的发请求的服务名（`llm`/`finance`），没有则为空数组 |
| `budget_exceeded` | `bool` | 机构今日 token 预算是否已用完导致跳过 LLM 调用 |
| `worker_id` | `str` | 处理这条消息的 worker 容器 hostname（`app/worker/graph/graph.py:36`） |
| `path` | `[str]` | 这条消息实际经过的图节点名，按执行顺序，末尾固定加 `respond`（`respond()` 不是 StateGraph 节点） |
| `timings` | `{节点名: 毫秒}` | 每个节点自己的耗时 |
| `llm_ms` | `float` | 这条消息真正花在等 LLM 网络调用上的总时间（毫秒），累加自 classify/handoff/respond 三处，没调用 LLM 是 `0`（不是 `null`） |

`README.md`"`reply_end` 的 `meta` 字段"一节列了前 12 个字段，没有列 `worker_id`/`path`/
`timings`/`llm_ms` 这四个（阶段三 3.8 才加，供演示控制台流程图回放用）——本文档是权威版本。

### 重连与多端同步

gateway 不维护会话级的服务端状态（无状态，`app/gateway/main.py` 没有为每个连接保留业务
数据）。同一个 `(tenant_id, user_id)` 可以同时开多个 WebSocket 连接（多端）：
`ConnectionManager` 按 `(tenant_id, user_id)` 分组维护连接集合，第一个连接建立时才订阅对应
的 Redis 频道 `im:out:{tenant_id}:{user_id}`，最后一个断开时才退订；频道收到的每条推送会
发给这个用户当前所有本地连接（`app/gateway/connection_manager.py:27-56,88-102`）——这就是
多端同步：同一个账号的手机和网页会同时收到同一条回复。

断线重连：客户端重新连接后重复走一遍鉴权流程；`message_id`/`conversation_id` 由客户端自己
延续，服务端凭这两者判断历史归属，不需要额外的重连协议。gateway 订阅 Redis 频道本身如果断线
（Redis 重启），按指数退避重连（`REDIS_RECONNECT_MIN_SECONDS` 到 `REDIS_RECONNECT_MAX_SECONDS`
翻倍增长，`app/gateway/connection_manager.py:70-119`），期间该用户收不到新推送，但已入库的回复
不会丢，重连后可以通过历史消息接口/UI 重新看到。

## HTTP 接口

### `/health`、`/ready`、`/metrics`（gateway/worker/scheduler，无鉴权）

- `GET /health`：gateway 会检查 Redis 是否可用，`redis` 挂了仍返回 200，`status` 标
  `degraded`（`app/gateway/main.py:38-44`，设计见 ARCHITECTURE.md 第 16 条）；worker/
  scheduler 的 `/health` 恒返回 `{"status": "ok"}`（`app/worker/main.py:19-21`、
  `app/scheduler/main.py:19-21`，进程能响应就说明没死）。
- `GET /ready`（仅 gateway）：检查 PostgreSQL、Redis、RabbitMQ 三者，任一不可用返回 503
  （`app/gateway/main.py:47-54`）。
- `GET /metrics`：Prometheus 格式指标，三个服务都有（`app/gateway/main.py:57-59`、
  `app/worker/main.py:24-26`、`app/scheduler/main.py:24-26`）。

### 演示控制台后端（mock-im，`mocks/mock_im/main.py`）

只做跟业务无关的辅助事：发页面、签发演示用 token、给控制台查状态/摘要/工单/审计日志。

| 接口 | 鉴权 | 说明 |
|---|---|---|
| `GET /` | 无 | 返回演示控制台页面（`templates/index.html`） |
| `GET /health` | 无 | 恒返回 `{"status":"ok"}` |
| `GET /api/token?tenant_id=&user_id=` | 无（这是"扮演登录系统签发 token"的接口本身） | 现查数据库拿 `role`，签发 JWT；用户不存在返回 404 |
| `GET /api/status?tenant_id=&token=` | **有**：`_require_same_tenant()`——token 必须有效且所属机构等于 `tenant_id`（`main.py:73-84,111-118`） | 查 gateway `/health` + 该机构今日 token 用量/预算 |
| `GET /api/conversation/context?tenant_id=&conversation_id=&token=` | **有**：`_require_same_tenant()` + 校验会话归属（`conversations.tenant_id`/`user_id` 必须等于 token 里的值，`main.py:157-165`） | 查该会话的历史摘要 |
| `GET /api/handoff_tickets?tenant_id=&token=` | **有**：`_require_agent()`——角色必须是 `agent` 且机构匹配（`main.py:87-100`） | 本机构最近 50 条转人工工单 |
| `GET /api/audit_logs?tenant_id=&token=` | **有**：`_require_agent()` | 本机构最近 50 条审计日志（`detail` 过一遍 `mask_text` 脱敏） |

`/api/status`、`/api/conversation/context` 这两个接口的鉴权是阶段二第二轮人工审查后补上的
（`AGENT_LOG.md` 索引第 18 条：最初任何人传对 `tenant_id`/`conversation_id` 就能查到别的
机构/别人的数据）。

### mock-platform（`mocks/mock_platform/main.py`）

| 接口 | 鉴权 | 说明 |
|---|---|---|
| `POST /commands` | 无（内部服务，靠 docker 网络隔离） | body：`{tenant_id, user_id, action, params, idempotency_key}`；同一个 `idempotency_key` 重复调用直接返回第一次的结果，不重新执行（`main.py:150-182`） |
| `GET /users/{user_id}/subscriptions?tenant_id=` | 无 | 返回该用户名下课程的自动续费状态 |
| `GET /admin/commands` | 无 | 列出所有真正执行过的指令（按幂等键去重后），供验证幂等用 |
| `GET /agents/status` | 无 | `{"online","queue_length","avg_wait_minutes"}` |
| `GET/POST /admin/config`、`POST /admin/reset` | 无 | 调试用：切故障模式（`normal`/`timeout`/`slow_commit`/`error500`）、坐席在线状态、排队参数 |

### mock-finance（`mocks/mock_finance/main.py`）

`/orders`、`/bills`、`/invoices`、`/refunds`、`/balance` 五个查询接口签名一致：

```
GET /orders?user_id=<目标用户id>&period=<可选>
Headers: X-Service-Token: <FINANCE_SERVICE_TOKEN>
         X-Tenant-Id: <tenant_id>
         X-Acting-User-Id: <发起查询的用户id>
```

三层校验（`main.py:228-243,273-280`）：`X-Service-Token` 不对 → 401；`X-Tenant-Id`/
`X-Acting-User-Id`/`user_id` 不在同一租户或不满足"查自己"/"家长查关联学员" → 403；查无此人
的账期 → 200 但返回空列表（不是 404，避免暴露"这个人存不存在"）。这是 worker 侧权限判断之外
独立的第二层（`app/worker/graph/finance.py:1-6`），就算 worker 判断出错，这层还能兜底拦一次。

### mock-llm（`mocks/mock_llm/main.py`）

OpenAI 兼容：`POST /v1/chat/completions`（支持 `stream`/`tools`/`tool_choice`），工具调用走
`mocks/mock_llm/rules.py` 的确定性规则，不是真实模型推理。`GET/POST /admin/config`、
`POST /admin/reset` 切故障模式（`normal`/`hallucinate`/`invalid_json`/`ai_flavor`/
`error500`）、延迟（`latency_ms`）、`error_rate`、`timeout_rate`（挂起不返回，区别于
`error_rate` 立即 500，见 `main.py:33-37`）。

### mock-knowledge（`mocks/mock_knowledge/main.py`）

`GET /search?q=<查询>&tenant_id=<租户>&top_k=3`：读同一份 `data/knowledge/{tenant}/*.md`
文件，用双字片段（bigram）Jaccard 相似度打分（跟 pgvector 检索器的打分尺度不同，见
`docs/phase2_threshold.md`），返回 `{"results":[{"snippet","source":{"doc_id","title",
"clause_no"},"score"}]}`。只在 `RETRIEVER=mock_knowledge` 时被 worker 使用，默认走
pgvector 不经过这个服务。
