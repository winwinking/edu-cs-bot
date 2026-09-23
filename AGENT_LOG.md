# AGENT_LOG

记录每个已完成步骤改了什么、关键设计决策是什么。"人工审查与修复点"由 Jo 填写。

---

## 步骤 1.1：仓库初始化

**日期**：2026-09-23

**改动/新建模块**：
- 仓库骨架目录：`app/{common,gateway,worker,scheduler}`、`mocks/{mock_im,mock_llm,mock_knowledge,mock_platform,mock_finance}`、`docker/`、`migrations/`、`scripts/`、`tests/`
- `Makefile`（up/down/logs/ps/migrate/seed/demo 已接线，test/loadtest 占位打印"待实现"）
- `docker-compose.yml`（骨架，services 留空，步骤 1.2 补基础设施）
- `docker/app.Dockerfile`、`docker/mocks.Dockerfile`
- `requirements.txt`、`.env.example`、`.gitignore`、`.gitattributes`
- `README.md`（占位版）
- `alembic.ini`（骨架，步骤 1.4 接入真实 DB 配置）

**关键决策**：
- `.env.example` 一次性列全了整个阶段一会用到的变量（DB/Redis/MQ/JWT/LLM/端口/应用行为），这样后面每一步新增代码时只管从 `Settings` 读，不会出现"漏了某个变量才发现要改 .env.example"的情况。
- gateway/worker/scheduler 共用 `app.Dockerfile`，靠 docker-compose 里不同的 `command` 区分启动的是哪个服务，避免维护三份几乎一样的 Dockerfile。
- `Makefile` 里的 `migrate`/`seed`/`demo` 已经把命令接好，但依赖的脚本和服务要到 1.4/1.8 才会真正存在，现在跑会失败，这是预期的（结构先行）。

**人工审查与修复点**：
（留空，由 Jo 填写）

---

## 步骤 1.2：基础设施容器

**日期**：2026-09-23

**改动/新建模块**：
- `docker-compose.yml`：加入 `postgres`（pgvector/pgvector:pg15）、`redis`（redis:7-alpine，开 appendonly）、`rabbitmq`（rabbitmq:3.13-management）三个服务，都带 healthcheck，密码/端口全部走 `${VAR}` 从 `.env` 取值
- 三个服务各自加了具名 volume（`postgres_data`/`redis_data`/`rabbitmq_data`）做数据持久化

**关键决策**：
- healthcheck 分别用 `pg_isready`、`redis-cli ping`、`rabbitmq-diagnostics -q ping`——这三个是官方镜像自带的探活命令，不用额外装东西，也符合 PHASE1 里点名要求的方式。
- 端口都用 `${XXX_HOST_PORT}` 变量映射而不是写死，本机已经占用某个端口时改 `.env` 就行，不用碰 compose 文件。
- 暂时没有给 postgres/redis/rabbitmq 加 `depends_on`（它们互相不依赖），`depends_on: condition: service_healthy` 会在步骤 1.6/1.7 给 gateway/worker 加上，等它们真正依赖这三个基础设施时再接。

**验证记录**：
- `docker compose config --quiet` 通过
- `make up` 拉镜像、启动，`docker compose ps` 三个服务均 `healthy`
- `curl http://localhost:15672` 返回 HTTP 200（RabbitMQ 管理界面可访问）
- 验证完后执行了 `docker compose down`（保留数据卷，未清理），需要的话可以再 `make up` 拉起来看

**人工审查与修复点**：
- Jo 发现：本机之前做图谱项目时装过一个开机自启的原生 Redis，同样占 6379 端口。容器和它都能各自启动，但本机脚本连 `localhost:6379` 时可能悄悄连到那个原生 Redis 而不是容器里的这个，排查起来会很晕。
- 处理：`.env.example`/`.env` 的 `REDIS_HOST_PORT` 默认值从 6379 改成 6380（仅宿主机映射端口，容器内部互联仍是标准的 6379，不影响 gateway/worker 的连接方式），并在 README 端口表里写明原因，避免以后忘记为什么用了非标准端口。

---

## 步骤 1.3：公共模块 app/common

**日期**：2026-09-23

**改动/新建模块**：
- `app/common/config.py`：pydantic-settings 的 `Settings`，把 `.env.example` 里列的所有变量都建了模型，`get_settings()` 用 `lru_cache` 保证全进程只解析一次
- `app/common/logging.py`：structlog 配置（JSON 输出、`ensure_ascii=False`），`bind_trace_context`/`clear_trace_context` 用 contextvars 绑定 trace_id/tenant_id/conversation_id；加了 `desensitize_processor`，用正则在渲染前脱敏手机号、身份证、银行卡、邮箱、JWT 形态字符串
- `app/common/db.py`：async engine + `AsyncSessionLocal` 工厂，`Base`（给 1.4 的 ORM 模型用），`check_db_connection()` 给 `/ready` 探活
- `app/common/redis.py`：`redis.asyncio` 客户端（带超时），`check_redis_connection()`
- `app/common/mq.py`：aio-pika 连接 + 拓扑声明（`im.inbound` → `inbound.messages`，死信 `im.dlx` → `inbound.dead`，`inbound.messages` 配置了 dead-letter 参数），开启 publisher confirms
- `app/common/auth.py`：PyJWT 签发/校验（HS256），claims 是 sub/tenant_id/role/exp，校验失败统一抛 `TokenError`
- `app/common/schemas.py`：WebSocket 消息协议的 Pydantic 模型（`ClientMessage`/`AckMessage`/`ReplyChunkMessage`/`ReplyEndMessage`/`ErrorMessage`），`ClientMessage.content` 超过 `WS_MESSAGE_MAX_LENGTH` 直接校验失败
- `app/common/llm_client.py`：`AsyncOpenAI` 封装 + `stream_chat_completion()` 流式辅助函数，base_url/api_key/model/timeout 全部来自配置
- `.env.example`/`.env`：补了 `DB_CONNECT_TIMEOUT_SECONDS`/`REDIS_TIMEOUT_SECONDS`/`MQ_CONNECT_TIMEOUT_SECONDS` 三个超时配置项

**关键决策**：
- 所有外部调用的超时都做成了独立的环境变量（而不是写死的魔法数字），跟已有的 `LLM_TIMEOUT_SECONDS` 保持同样风格，对应硬性规则"所有外部调用必须有超时"。
- 日志脱敏用正则按"先处理更具体、不容易和别的数字串混淆的模式"的顺序（JWT → 邮箱 → 身份证 → 手机号 → 银行卡），是启发式做法不是 100% 精确，但覆盖了需求里点名的几类敏感信息；处理的是整个 event_dict 的每个字段值（含主 message），不止是最外层字符串。
- `llm_client.py` 除了裸的 `AsyncOpenAI` 实例，还加了一个 `stream_chat_completion()` 薄封装，worker（步骤 1.7）只管消费文本分片，不用直接碰 OpenAI SDK 的流式接口细节。
- `mq.py` 的 `get_confirm_channel` 显式传 `publisher_confirms=True`，配合 gateway（步骤 1.6）"等 confirm 成功才回 ack"的设计。

**验证记录**：
- 起了一个干净的 `python:3.11-slim` 容器（跟 compose 同一个 docker network，装了 `requirements.txt`），跑了一遍脚本：
  - `Settings` 能正确加载 `.env` 里的值（含刚改的 `REDIS_HOST_PORT=6380`）
  - 日志脱敏：手机号 `138****5678`、邮箱 `j***@example.com`、伪造 JWT 被整体替换成 `***REDACTED_TOKEN***`、身份证 `110101********2316`，且 trace_id/tenant_id/conversation_id 正确挂在日志上
  - JWT 签发后能正确解出 sub/tenant_id，篡改后的 token 被 `TokenError` 正确拒绝
  - `ClientMessage` 正常内容能解析，超长内容（3000 字符）被 Pydantic 校验拒绝
  - `check_db_connection()` / `check_redis_connection()` 对着真实跑起来的 postgres/redis 都返回 True
  - `declare_topology()` 对着真实 RabbitMQ 成功声明出 `inbound.messages` 和 `inbound.dead` 两个队列
  - 全部检查项打印 `ALL CHECKS PASSED`
- 验证完执行了 `docker compose down`

**人工审查与修复点**：
（留空，由 Jo 填写）
