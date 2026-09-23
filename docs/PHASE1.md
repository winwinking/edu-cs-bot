# PHASE 1：骨架与消息全链路

## 阶段目标
`make up` 一键启动所有服务。一条消息完整走通：
客户端 WebSocket 发消息 → gateway 鉴权、校验、去重 → 投递 RabbitMQ → 收到队列确认后 ACK 客户端 → worker 消费 → 调用 mock-llm（流式）→ 回复分片经 Redis 推回 gateway → 推送给客户端。

**本阶段不做**：意图路由、RAG、财务、指令、提醒、限流熔断（阶段二、三再做）。

---

## 设计假设（写进 README）
gateway 直接作为 IM 长连接入口；mock-im 扮演 IM 客户端（一个简单网页聊天界面），用于演示和手工测试。

---

## 步骤 1.1：仓库初始化
目录结构：
```
edu-cs-bot/
├── CLAUDE.md
├── AGENT_LOG.md
├── README.md
├── Makefile
├── docker-compose.yml
├── .env.example
├── .gitignore            # 必须包含 .env、__pycache__、.venv、*.pyc、.pytest_cache、.coverage
├── .gitattributes        # * text=auto eol=lf
├── requirements.txt
├── docker/
│   ├── app.Dockerfile     # gateway / worker / scheduler 共用
│   └── mocks.Dockerfile   # 所有 mock 共用
├── alembic.ini
├── migrations/
├── app/
│   ├── common/            # config, logging, db, redis, mq, auth, schemas, llm_client
│   ├── gateway/
│   ├── worker/
│   └── scheduler/         # 本阶段只放占位
├── mocks/
│   ├── mock_im/
│   ├── mock_llm/
│   ├── mock_knowledge/    # 本阶段只有 /health
│   ├── mock_platform/     # 本阶段只有 /health
│   └── mock_finance/      # 本阶段只有 /health
├── scripts/
├── tests/
└── docs/
    ├── REQUIREMENTS.md
    └── PHASE1.md
```
Makefile 目标：`up`（docker compose up -d --build）、`down`、`logs`、`ps`、`migrate`、`seed`、`demo`、`test`、`loadtest`（后两个本阶段先占位，打印"待实现"）。

**验证**：`git status` 看不到 `.env`；`.env.example` 包含所有需要的变量且值都是示例值。

---

## 步骤 1.2：基础设施容器
docker-compose.yml 先加入：
- `postgres`：镜像 `pgvector/pgvector:pg15`，数据卷持久化，healthcheck 用 `pg_isready`
- `redis`：`redis:7-alpine`，开启 appendonly，healthcheck 用 `redis-cli ping`
- `rabbitmq`：`rabbitmq:3.13-management`，开放管理界面端口 15672，healthcheck 用 `rabbitmq-diagnostics -q ping`

所有密码从 `.env` 读取。应用服务用 `depends_on: condition: service_healthy` 等待依赖就绪。

**验证**：`make up` 后 `docker compose ps` 全部 healthy；浏览器能打开 RabbitMQ 管理界面。

---

## 步骤 1.3：公共模块 app/common
- `config.py`：pydantic-settings 的 Settings，所有配置从环境变量读取
- `logging.py`：structlog 输出 JSON；用 contextvars 绑定 trace_id / tenant_id / conversation_id；加一个脱敏 processor，用正则把日志里的手机号、身份证、银行卡、邮箱、JWT 形态字符串打码
- `db.py`：async engine + session 工厂
- `redis.py`：redis.asyncio 客户端（带超时）
- `mq.py`：aio-pika 连接；声明拓扑：
  - exchange `im.inbound`（direct，durable）→ queue `inbound.messages`（durable）
  - 死信 exchange `im.dlx` → queue `inbound.dead`
  - `inbound.messages` 配置 `x-dead-letter-exchange=im.dlx`
  - 开启 publisher confirms
- `auth.py`：JWT 签发与校验（PyJWT，HS256，密钥来自环境变量）。claims：`sub`(user_id)、`tenant_id`、`role`、`exp`
- `schemas.py`：WebSocket 消息协议（见下）
- `llm_client.py`：AsyncOpenAI 封装，`base_url`、`api_key`、`model`、超时全部来自配置。切换 mock-llm 和 DeepSeek 只改环境变量，不改代码

### WebSocket 消息协议
客户端 → gateway：
```json
{"type": "message", "message_id": "uuid", "conversation_id": "string", "content": "string"}
```
gateway → 客户端：
```json
{"type": "ack", "message_id": "...", "status": "accepted | duplicate", "trace_id": "..."}
{"type": "reply_chunk", "reply_to": "message_id", "seq": 0, "delta": "..."}
{"type": "reply_end", "reply_to": "message_id"}
{"type": "error", "message_id": "...", "code": "...", "detail": "..."}
```
content 长度上限（如 2000 字符），超出返回 error，不入队。

---

## 步骤 1.4：数据库迁移与种子数据
Alembic 迁移创建：
- `tenants`（id, name）
- `users`（id, tenant_id, name, role, email, phone, created_at）；role 取值：student / parent / agent / admin
- `conversations`（id, tenant_id, user_id, created_at, updated_at）
- `messages`（id, tenant_id, conversation_id, message_id, role[user/assistant], content, trace_id, created_at）；
  **唯一约束 (tenant_id, message_id)**，这是 worker 端幂等的最后一道防线
- 启用 `CREATE EXTENSION IF NOT EXISTS vector`（阶段二用）

`scripts/seed.py`：两个租户（`t_a` 星辰教育、`t_b` 启明学堂），每个租户 2~3 个用户，覆盖 student、parent、agent 角色。
`scripts/gen_token.py --tenant t_a --user u_a_1001`：打印一个有效 JWT，供手工测试。

**验证**：`make migrate && make seed` 成功；能进 psql 看到数据。

---

## 步骤 1.5：Mock 服务
### mock-llm（OpenAI 兼容）
- `POST /v1/chat/completions`，支持 `stream: true`（SSE 格式，`data: {...}`，最后 `data: [DONE]`）和非流式
- 行为可配置（环境变量默认值 + `POST /admin/config` 运行时修改）：
  - `latency_ms`：首 token 前延迟
  - `error_rate`：按比例返回 500
  - `mode`：`normal`（本阶段只实现 normal；`hallucinate`、`invalid_json` 留到阶段四故障注入时再实现，先预留枚举）
- normal 模式返回一段固定模板的客服风格回复，逐字/逐词分片输出
- `GET /health`

### mock-im
一个极简网页聊天界面（单个 HTML 页面，由 FastAPI 托管）：输入 token → 连接 gateway WebSocket → 发送消息、显示 ACK 和流式回复。每条消息客户端自动生成 message_id（uuid）。提供一个"重发上一条"按钮，用同一个 message_id 重发，用于演示去重。

### mock-knowledge / mock-platform / mock-finance
本阶段只实现 `GET /health`。

---

## 步骤 1.6：gateway
- `GET /health`（存活）、`GET /ready`（检查 PostgreSQL、Redis、RabbitMQ 是否可用）、`GET /metrics`
- `WS /ws?token=...`：
  1. 握手时校验 JWT，失败以 close code 4401 关闭；日志不记录 token
  2. 连接建立后，订阅 Redis 频道 `im:out:{tenant_id}:{user_id}`（同一用户多端连接都会收到 → 天然支持多端同步）；同一实例上该用户最后一个连接断开时退订
  3. 收到消息：Pydantic 校验 → 生成 trace_id → Redis `SET dedup:{tenant_id}:{message_id} NX EX 86400`
     - 已存在：回 `ack status=duplicate`，不入队
     - 不存在：投递 RabbitMQ（persistent 消息，headers 带 trace_id、tenant_id、user_id），**等 publisher confirm 成功后**才回 `ack status=accepted`
     - 投递失败：删除刚写的 dedup key（否则客户端重试会被误判为重复），回 error，让客户端重试
  4. 收到 Redis 频道消息 → 原样转发给该用户的 WebSocket 连接
- 指标：当前 WebSocket 连接数（gauge）、入站消息数（counter，按 status 分）、ACK 延迟（histogram）

---

## 步骤 1.7：worker
- 消费 `inbound.messages`，`prefetch_count` 从配置读取（默认 32）
- 每条消息：
  1. 从 headers 绑定 trace_id / tenant_id 到日志上下文
  2. 校验会话归属：conversation 不存在则以当前 (tenant_id, user_id) 创建；存在但属于其他用户/租户 → 拒绝处理并推送 error（越权）
  3. `INSERT` 用户消息到 messages 表，`ON CONFLICT (tenant_id, message_id) DO NOTHING`；如果没插入成功，说明已处理过 → 直接 ack 队列消息并跳过（**队列至少一次投递 + 数据库唯一约束 = 业务只处理一次**）
  4. 读取该会话最近 10 条消息作为上下文
  5. 调用 LLM（流式），每个分片发布到 `im:out:{tenant_id}:{user_id}`，结束发 `reply_end`
  6. 完整回复写入 messages 表（role=assistant）
  7. ack 队列消息
- 失败分两类：
  - **可预期的失败**（LLM 超时/500）：推送一条降级回复（"系统有点忙，我稍后再回复你，也可以回复'转人工'"），记录日志，ack 消息
  - **不可预期的异常**（消息格式损坏、代码 bug）：`reject(requeue=False)` → 进入死信队列 `inbound.dead`，记录 error 日志
- worker 同进程内起一个轻量 HTTP 服务（端口 8001）：`/health`、`/metrics`
- 指标：处理消息数（按结果分）、处理耗时 histogram、首 token 耗时 histogram

---

## 步骤 1.8：脚本与 make demo
- `scripts/ws_client.py`：命令行 WebSocket 客户端（参数：token、conversation_id、消息内容），打印 ACK 耗时、首 token 耗时、完整回复耗时
- `make demo`：自动生成 token → 发送一条消息 → 打印完整流程和三个耗时 → 用同一个 message_id 再发一次，展示 duplicate

---

## 步骤 1.9：README 初版
包含：项目简介、架构图（先用 Mermaid）、端口表、启动步骤（`cp .env.example .env` → `make up` → `make migrate` → `make seed` → `make demo`）、设计假设、目录说明。

---

## 阶段一验收清单（Jo 逐条验证）
- [ ] `make up` 所有容器 healthy
- [ ] `make migrate && make seed` 成功
- [ ] mock-im 页面能聊天，看到 ACK 和逐字流式回复
- [ ] "重发上一条"得到 duplicate，且只有一条回复
- [ ] 无 token / 错误 token 连接被拒
- [ ] RabbitMQ 管理界面能看到 `inbound.messages` 和 `inbound.dead`
- [ ] 日志是 JSON，带 trace_id，里面找不到 token
- [ ] 停掉 mock-llm（`docker compose stop mock-llm`）后发消息，收到降级回复，worker 不崩
- [ ] `make demo` 打印三个耗时
- [ ] `.env` 不在 git 里
