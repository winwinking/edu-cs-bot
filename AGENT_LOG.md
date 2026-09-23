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
- Jo 发现：脱敏只靠内容正则去猜格式，存在漏判风险——只要值的格式不标准（比如手机号存成非标准格式、token 不是 JWT 形态），正则就可能匹配不上，敏感信息照样原样进日志。
- 处理：`logging.py` 的 `desensitize_processor` 加了一层按字段名脱敏，key 里只要包含 `password`/`token`/`phone`/`email`/`id_card`/`bank_card`（大小写不敏感、子串匹配，如 `access_token`、`user_phone`、`bank_card_no` 都能命中），不管值是什么格式，整体替换成 `***REDACTED***`，作为第一道防线；原来的内容正则退化为兜底，负责处理敏感信息混在普通文本字段里的情况（比如 `detail: "用户手机号是 13812345678"`）。已用非标准格式的 password/token/phone/id_card/bank_card 值验证过，字段名这层确实能兜住正则漏判的场景。

---

## 步骤 1.4：数据库迁移与种子数据

**日期**：2026-09-23

**改动/新建模块**：
- `app/common/models.py`（新增，不在步骤 1.3 原定的文件清单里——见下面"偏离说明"）：`Tenant`/`User`/`Conversation`/`Message` 四张表的 ORM 模型，`UserRole`/`MessageRole` 两个 Python 枚举映射成 Postgres 原生 ENUM
- `migrations/env.py`：async Alembic 环境，连接串从 `Settings` 拿（不从 `alembic.ini` 读，避免密码进版本库），手动把 cwd 加进 `sys.path` 保证 `import app.*` 不失败
- `migrations/script.py.mako`：标准 Alembic 模板
- `migrations/versions/202609230001_init_schema.py`：建表迁移——启用 `vector` 扩展、建 `tenants`/`users`/`conversations`/`messages` 四张表，`messages` 上建 `(tenant_id, message_id)` 唯一约束，`users.tenant_id`、`conversations.(tenant_id, user_id)`、`messages.(tenant_id, conversation_id, created_at)` 都建了索引
- `scripts/seed.py`：种两个租户（t_a 星辰教育、t_b 启明学堂），每个租户 3 个用户（student/parent/agent 各一个），`ON CONFLICT DO NOTHING` 保证脚本可重复跑
- `scripts/gen_token.py`：按 `--tenant --user` 查数据库拿到真实 role，签发 JWT 打印出来
- `docker/app.Dockerfile`：加了 `ENV PYTHONPATH=/app`（见下面"过程中发现的 bug"）

**偏离 PHASE1 文档的地方**：
- 步骤 1.3 列的 `app/common` 文件清单里没有 `models.py`，但步骤 1.4 要建表、种子脚本和 `gen_token.py` 都要查用户表，需要有个地方放 ORM 模型定义，所以补了这个文件，放在 `app/common` 下（worker 步骤 1.7 之后也会用到同一套模型）。这是为了完成步骤 1.4 必须做的最小补充，没有多做别的。

**关键决策**：
- 迁移是手写的 `op.create_table`，没有直接用 `alembic revision --autogenerate` 生成正式迁移——手写的能加中文注释解释为什么有唯一约束、为什么建这些索引，autogenerate 生成的东西没法加注释还很啰嗦。但写完之后专门跑了一次 autogenerate 做"一致性检查"：如果 `models.py` 和手写迁移之间有差异，autogenerate 会生成非空的 diff；跑出来是空的（`upgrade()`/`downgrade()` 都是 `pass`），说明两边完全对得上，这个临时生成的文件验证完就删了，不进版本库。
- `tenants.id`/`users.id` 用业务可读的字符串主键（`t_a`、`u_a_1001`），不用自增数字或 UUID——这两张表的行是人工/种子数据定的，可读性对手工测试和讲解更重要；`conversations.id`/`messages.id` 是系统运行时生成的，用 UUID（Python 侧 `uuid.uuid4` 默认值，不依赖数据库端生成函数）。
- `role` 字段用 Postgres 原生 ENUM 而不是普通字符串 + 应用层校验，非法角色在数据库这一层就会被拒绝，不用等到业务代码校验。
- `(tenant_id, message_id)` 唯一约束的注释直接抄了需求原文的表述："队列至少一次投递 + 数据库唯一约束 = 业务只处理一次"，方便讲解时对照。

**过程中发现的 bug（自己验证时发现并修复，不是 Jo 发现的）**：
1. 迁移脚本一开始把同一个 `postgresql.ENUM(...)` 对象既用来手动 `.create()`，又用在 `Column` 定义里——`create_table` 建表时会对列关联的类型再自动建一次，导致 `DuplicateObjectError: type "user_role" already exists`。修法：给这个 ENUM 对象传 `create_type=False`，告诉它"类型我自己管，你建表时别自动建"。
2. `python scripts/seed.py` 这样直接跑脚本时，Python 会把 `sys.path[0]` 设成脚本所在目录 `scripts/`，不是项目根目录 `/app`，导致 `import app.common...` 报 `ModuleNotFoundError`。修法：在 `app.Dockerfile` 里加 `ENV PYTHONPATH=/app`，让容器里所有 `python scripts/xxx.py` 都能正常 import，不用每个脚本自己写 `sys.path.insert`。

**验证记录**：
- 用真实 `python:3.11-slim` 容器（接到 compose 网络）跑了一遍完整流程：
  - `alembic upgrade head` 成功；查 `information_schema` 确认 5 张表（含 `alembic_version`）都建出来了，`uq_messages_tenant_message_id` 唯一约束存在，`vector` 扩展已启用
  - `alembic revision --autogenerate` 生成空 diff，验证 `models.py` 和手写迁移完全一致，验证完删掉了这个临时文件
  - `scripts/seed.py` 跑了两次，两次都是"2 个租户，6 个用户"，确认幂等；查出来的用户角色分布是 t_a/t_b 各 1 个 student/parent/agent，跟种子数据设计一致
  - `scripts/gen_token.py --tenant t_a --user u_a_1001` 打出的 JWT 解码后 `sub`/`tenant_id`/`role` 三个 claim 都对；对不存在的用户 `u_nope` 正确抛错退出
  - 全部打印 `ALL MIGRATION/SEED CHECKS DONE`
- 验证完执行了 `docker compose down`

**人工审查与修复点**：
CC 验证时未走 make migrate / make seed，实际执行因依赖尚未创建的 worker 服务而失败。已要求改为独立的一次性 tools 服务，并按用户真实操作路径重新验证。

**补充修复（同一次审查里一起改的）**：
- `docker-compose.yml` 新增 `tools` 服务：跟 gateway/worker/scheduler 共用 `docker/app.Dockerfile`，打了 `edu-cs-bot/app:latest` 镜像标签方便以后复用；`profiles: ["tools"]` 让它不随 `make up`/`docker compose up` 启动，只有显式 `docker compose run --rm tools ...` 才会被叫起来，干完活自动退出（`--rm`）；`depends_on: postgres: condition: service_healthy` 保证迁移/种子脚本不会在数据库还没就绪时跑。
- `Makefile` 的 `migrate`/`seed` 目标从 `docker compose run --rm worker ...` 改成 `docker compose run --rm tools ...`。
- 用真实命令重新走了一遍 `make up → make migrate → make seed → make seed`：
  - `make up`（`docker compose up -d --build`）确认没有把 `tools` 一起建出来/启动（`profiles` 生效）
  - `make migrate` 第一次跑要现场 build `tools` 镜像（约 3.5 分钟，纯下载依赖的时间），之后 `alembic upgrade head` 成功
  - `make seed` 跑两次，两次都打印"2 个租户，6 个用户"，确认幂等
  - `docker compose ps -a` 确认 `tools` 容器跑完即被移除，不会常驻
- 顺带确认了 `logging.py` 的按字段名脱敏（`_SENSITIVE_FIELD_RE`/`_REDACTED_FIELD`）确实还在，没有被后续改动带丢。

---

## 步骤 1.5：Mock 服务

**日期**：2026-09-23

**改动/新建模块**：
- `mocks/mock_llm/main.py`：OpenAI 兼容的 `POST /v1/chat/completions`（支持 `stream: true` 的 SSE 和非流式），`GET /admin/config` + `POST /admin/config` 运行时改 `latency_ms`/`error_rate`/`mode`，`GET /health`。`mode` 用 `Literal["normal","hallucinate","invalid_json"]` 先占好枚举，本阶段只实现 `normal`
- `mocks/mock_im/main.py` + `mocks/mock_im/templates/index.html`：一个纯前端的网页聊天客户端，`GET /` 把 `GATEWAY_HOST_PORT` 注入页面模板；页面 JS 直接连 gateway 的 WebSocket，不经过 mock-im 后端；带"重发上一条"按钮（复用同一个 message_id）
- `mocks/mock_knowledge/main.py`、`mocks/mock_platform/main.py`、`mocks/mock_finance/main.py`：本阶段只有 `GET /health`
- `docker-compose.yml`：加了 5 个 mock 服务，用 `x-mocks-build`/`x-mocks-healthcheck` 两个 YAML anchor 避免每个服务重复写同样的 build/镜像/健康检查配置

**关键决策**：
- mock 服务不依赖 `app.common`（`mocks.Dockerfile` 只拷贝 `mocks/` 目录，没拷贝 `app/`），行为配置直接读 `os.getenv`，不复用 `app.common.config.Settings`——mock 服务概念上是"假的外部系统"，跟我们自己的服务应该是完全独立可替换的，不应该和内部代码耦合。
- mock-im 页面里的 WebSocket 连接用的是 `window.location.hostname` + 注入的 `GATEWAY_HOST_PORT`（宿主机映射端口），不是容器内部端口——因为这段 JS 是在你的浏览器里跑的，浏览器在宿主机上，不在 docker network 里，必须用宿主机能访问到的地址。
- mock-llm 流式回复是逐字符 yield，字符间隔硬编码 30ms（纯粹为了演示打字机效果），只有"首 token 前延迟"（`latency_ms`）做成了可配置项，符合需求原文的措辞。
- 5 个 mock 服务共用 `docker/mocks.Dockerfile`，用 `command` 区分启动哪个模块，用 YAML anchor 避免 compose 文件里 5 份几乎一样的 build/healthcheck 配置。

**验证记录**：
- `docker compose config --quiet` 通过，`docker compose config` 确认 anchor 合并结果正确（image/build/restart/healthcheck 都在）
- `make up` 建出全部 8 个服务（3 个基础设施 + 5 个 mock），全部 `healthy`；因为 `mocks.Dockerfile` 和 `app.Dockerfile` 的 `pip install` 层内容完全一样，命中了 `tools` 镜像构建时留下的 Docker 层缓存，5 个 mock 镜像秒建
- `curl /health` 5 个 mock 服务全部返回 `{"status":"ok"}`
- mock-llm：非流式请求返回正确的模板回复；流式请求收到多个 `data: {...}` chunk（逐字），最后是 `finish_reason: stop` 的 chunk 加 `data: [DONE]`；`POST /admin/config` 改 `latency_ms=0` 后流式明显变快；改 `error_rate=1.0` 后请求正确返回 `500` + `mock-llm 模拟的上游错误`；改回默认值后恢复正常
- mock-im：`curl /` 确认页面里的占位符被替换成了真实的 `GATEWAY_HOST_PORT=8000`（gateway 还没实现，完整的连接收发要等步骤 1.6/1.8 才能测）
- 验证完执行了 `docker compose down`

**人工审查与修复点**：
（留空，由 Jo 填写）
