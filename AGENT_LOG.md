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
Jo 手工测试时发现：mock-im 发消息后 ack 只出现一次，但每个 reply_chunk 和 reply_end 都出现两次，文字变成"您您好好"这种逐字重复。Jo 提出两个排查方向：① 页面重复点连接导致同一页面挂两个 WebSocket；② gateway 的 ConnectionManager 单连接下也重复推送。已排查并修复，属于①。

**排查过程**：
- 先用真实容器验证②：单个 Python WebSocket 客户端连接、发一条消息，统计收到的 `reply_chunk`/`reply_end` 数量——结果是 83 个 chunk（跟回复字数一致）、`reply_end` 恰好 1 次，`reply_end` 之后再等 1.5 秒也没有多余消息。**排除了 gateway/worker 端重复推送的可能**。
- 再用两个 Python WebSocket 客户端模拟"同一个 token 连了两次、旧连接没关掉"的场景（`conn_a` 模拟按钮第一次点击留下的旧连接，`conn_b` 模拟第二次点击、页面变量实际指向的新连接，只用 `conn_b` 发送消息）：`conn_b` 收到 1 次 ack（跟 Jo 报告的"ack 只出现一次"吻合），`conn_a`/`conn_b` **各自**收到完整的 83 个 chunk 和 1 次 `reply_end`——如果页面把两个连接的 `onmessage` 都接到同一套共享 DOM 状态（`bubblesByReplyTo`、`logEl`）上，就会变成每个字都被追加写入两次、"回复结束"打印两次，跟 Jo 报告的现象完全对应。**复现了①的机制**。
- 回头看 `mocks/mock_im/templates/index.html` 的 connect 按钮逻辑：`connectBtn.disabled = true` 只在 `ws.onopen`（异步，等 WebSocket 握手完成才触发）里执行，握手完成前如果按钮又被点一次（连点、或者上一次连接还没握手完成又点了一次），就会创建第二个 `WebSocket` 对象，旧对象的 `onmessage` 也还活着，两个连接会同时被 gateway 转发同一份回复到同一套共享 DOM 状态里。

**处理**：`mocks/mock_im/templates/index.html` 的 connect 按钮点击逻辑改成：同步禁用按钮（不等 `onopen`）+ 如果已经有一个 `ws` 对象，先把它的四个事件处理器都置空再 `close()` 掉，再创建新连接。这样不管是连点两下还是别的什么原因导致 connect 被触发第二次，旧连接都会被立刻切断且不再往共享 DOM 状态里写东西。

**验证**：改完之后重新 build 了 `mocks` 镜像、重启 mock-im 容器，`curl http://localhost:8080/` 确认新逻辑已经在服务的页面里生效。浏览器里的连点场景没有用工具复现（没有可用的浏览器自动化工具），这部分麻烦 Jo 手工连点 2~3 次"连接"按钮确认不再重复。

**另外确认**：`mock-llm` 的 `latency_ms` 已经是默认值 300（`GET /admin/config` 返回 `{"latency_ms":300,...}`）——之前排查幂等漏洞时改到过 15000，但那次验证之后整个技术栈被 `docker compose down` 过，容器内的运行时状态（包括 `/admin/config` 改的值）不会跨这次 down/up 保留，新起的容器会重新从 `.env` 里的 `MOCK_LLM_LATENCY_MS=300` 初始化，不会带着旧值。

---

## 步骤 1.6：gateway

**日期**：2026-09-23

**改动/新建模块**：
- `app/gateway/main.py`：FastAPI app，`lifespan` 里建 MQ 连接、开 publisher-confirm channel、声明拓扑，存在 `app.state` 上；`GET /health`（存活）、`GET /ready`（查 postgres/redis/rabbitmq）、`GET /metrics`（Prometheus 文本格式）；`WS /ws?token=...` 端点
- `app/gateway/connection_manager.py`：`ConnectionManager`，按 `(tenant_id, user_id)` 分组管理本实例的连接集合，第一个连接建立时订阅 Redis 频道 `im:out:{tenant_id}:{user_id}`，最后一个断开时退订，订阅到的消息原样转发给这个用户当前所有本地连接
- `app/gateway/message_handler.py`：单条消息的处理流程——JSON 解析 → Pydantic 校验 → `SET dedup:{tenant_id}:{message_id} NX EX` 去重 → 发布到 RabbitMQ（等 publisher confirm）→ 回 ack；任何一步失败都回 `error`，投递失败时把刚写的 dedup key 删掉
- `app/gateway/metrics.py`：三个指标——`gateway_ws_connections`（gauge）、`gateway_inbound_messages_total{status}`（counter）、`gateway_ack_latency_seconds`（histogram）
- `app/common/mq.py`：`declare_topology` 返回值从 `(inbound_queue, dead_queue)` 改成 `(inbound_exchange, inbound_queue, dead_queue)`——gateway 要用 exchange 发消息，之前只有 worker 视角（只消费队列），漏了这个返回值
- `docker-compose.yml`：加了 `gateway` 服务；把 `tools` 的 build/image 配置提成 `x-app-build` anchor 复用（之前是 `tools` 自己写的，现在 gateway 也要用同一个镜像）

**关键决策**：
- WebSocket 的鉴权顺序是"先 `accept()`，鉴权失败再 `close(code=4401)`"，不是先鉴权失败直接拒绝握手——这是我自己验证时踩的坑，见下面"过程中发现的 bug"。
- 去重用 `SET NX EX` 一条 Redis 命令完成"不存在就写入并设过期"，这是原子操作，不会有"两个并发请求都查到不存在，都插入"的竞态。
- 消息体（发到 RabbitMQ 的）里放了 worker 需要的全部信息（tenant_id/user_id/conversation_id/message_id/content/trace_id），headers 里只放 trace_id/tenant_id/user_id 这三个（按需求原文），worker 不用回头查 gateway 拿上下文。
- `ConnectionManager` 用 `asyncio.Lock` 保护"连接集合从空变非空/从非空变空"这个判断，因为同一个用户可能有多个端几乎同时连接/断开，不加锁可能出现重复订阅或该退订时没退订。

**过程中发现的 bug（自己验证时发现并修复，不是 Jo 发现的）**：
- 一开始的实现是"鉴权失败时不 `accept()`，直接 `close(code=4401)`"，想法是"握手都没完成就不算建立过连接"。但实测发现这样浏览器/客户端收到的是 HTTP 403，收不到 4401——因为 WebSocket 的 close code 是协议帧的一部分，只有完成握手（`accept()`）之后才能带自定义 code 发出去，`accept()` 之前调 `close()` 会被 ASGI server 降级成普通的 HTTP 级别拒绝，自定义 code 传不出去。修法：改成先 `accept()`，鉴权失败立刻 `close(code=4401)`，这样客户端才能真的看到 4401。

**验证记录**：
- 用真实容器跑了一遍（`gateway` 容器 + `websockets`/`redis` 库直连测试脚本，都在 compose 网络里）：
  - `GET /health`、`GET /ready`（三个依赖都 `true`）、`GET /metrics`（能看到三个自定义指标）都正常
  - 不带 token 连接 / 带一个乱造的假 token 连接，两种情况都收到 `close code=4401`
  - 正常发一条消息 → 收到 `ack status=accepted` 且带 `trace_id`；用同一个 `message_id` 重发 → 收到 `ack status=duplicate`
  - 发超长内容（3000 字符）→ 收到 `error code=invalid_message`；发不合法 JSON → 收到 `error code=invalid_json`
  - 手动往 Redis 频道 `im:out:{tenant_id}:{user_id}` `PUBLISH` 一条消息，确认连着的 WebSocket 原样收到（验证了 `ConnectionManager` 的订阅转发链路）
  - `rabbitmqctl list_queues` 确认 accepted 的那条消息真的躺在 `inbound.messages` 里（此时还没有 worker 消费，符合预期），`inbound.dead` 是空的
  - `redis-cli TTL dedup:...` 确认去重 key 的过期时间接近 86400 秒
  - 全部断开连接后 `/metrics` 里 `gateway_ws_connections` 回到 0，`gateway_inbound_messages_total` 按 `accepted=1/duplicate=1/error=2` 精确对上这次测试做的事
- 验证完执行了 `docker compose down`

**人工审查与修复点**：
（留空，由 Jo 填写）

---

## 步骤 1.7：worker

**日期**：2026-09-23

**改动/新建模块**：
- `app/worker/handler.py`：核心业务逻辑 `process_inbound_message`——解析会话归属（不存在则以当前 tenant_id/user_id 创建，存在但不属于当前用户则抛 `ConversationForbidden`）→ `INSERT ... ON CONFLICT DO NOTHING RETURNING` 判断是否已处理过 → 读最近 N 条历史（`CONVERSATION_HISTORY_LIMIT`）→ 调 LLM 流式 → 写 assistant 消息。所有"预期内"的结果（forbidden/duplicate/llm_degraded/ok）都在这个函数内部处理完并返回结果标签，不往外抛异常
- `app/worker/consumer.py`：消费 `inbound.messages`，`prefetch_count` 从配置读；`_on_message` 先做消息体的基本 JSON/字段校验（不合格直接 reject 进死信），再调用 `process_inbound_message`；只有真正跳出 `process_inbound_message`（比如数据库挂了）的异常才会被判定为不可预期异常并 reject 进死信
- `app/worker/pubsub.py`：往 `im:out:{tenant_id}:{user_id}` 推 `reply_chunk`/`reply_end`/`error`，复用 `app.common.schemas` 里跟 gateway 共享的协议模型，保证两边格式不会走样
- `app/worker/metrics.py`：`worker_messages_total{result}`、`worker_process_seconds`、`worker_first_token_seconds`
- `app/worker/main.py`：同进程内跑 MQ 消费循环 + 一个轻量 FastAPI（端口 8001）的 `/health` `/metrics`
- `docker-compose.yml`：加了 `worker` 服务，依赖 postgres/redis/rabbitmq/mock-llm 都健康才启动

**关键决策**：
- `process_inbound_message` 的设计原则是"预期内的结果都自己处理完，正常返回；只有真正的 bug 才抛异常"——这样 `consumer.py` 的 ack/reject 判断逻辑非常简单：调用不抛异常就 ack，抛了就 reject 进死信，不用在 consumer 里写一堆 `except SpecificError` 分支。
- LLM 调用特意挪到了数据库 session 外面单独做——流式请求可能要好几秒甚至更久，不能让一个数据库连接在这几秒里被占着不放。写 assistant 回复时会重新开一个新 session。
- 越权（`ConversationForbidden`）算"预期内"的业务结果，不进死信——死信是留给消息本身有问题（格式损坏、字段缺失、代码 bug）的，越权是正常业务逻辑能处理的一种情况，只是结果是拒绝而已，所以是 ack + 推 `error` 给客户端，不是 reject。
- assistant 消息插入时自己拼了个 `assistant-{uuid4()}` 当 `message_id`——因为 `(tenant_id, message_id)` 的唯一约束是给客户端消息去重用的，assistant 消息不是客户端发的，没有天然的 message_id，只要保证不撞车就行。

**验证记录（用真实容器 + 直连 WebSocket/RabbitMQ 的脚本，覆盖了阶段一验收清单里 worker 相关的每一条）**：
- 正常发一条消息 → gateway 收到 `ack accepted` → worker 消费、插入用户消息、调 mock-llm、逐字推 `reply_chunk`，客户端拼出来的完整回复跟 mock-llm 的模板一字不差，最后收到 `reply_end`
- 同租户下另一个用户拿别人的 `conversation_id` 发消息 → 收到 `ack accepted`（gateway 不做归属校验）之后紧接着收到 `error code=forbidden`；跨租户复用同一个 `conversation_id` 同样被拒绝——两种越权场景都验证了
- **停掉 mock-llm** 发消息 → openai SDK 自动重试两次后放弃，worker 捕获异常发送降级回复，文本跟需求原文一字不差（"系统有点忙，我稍后再回复你，也可以回复'转人工'"），**worker 容器全程保持 healthy，没有崩**；重新起 mock-llm 后恢复正常
- 绕开 gateway，直接往 `inbound.messages` 投递一条非 JSON 的消息体、一条缺字段的合法 JSON——`rabbitmqctl list_queues` 确认两条都进了 `inbound.dead`（分别验证），`inbound.messages` 保持 0，worker 日志里有对应的 error 级别记录且没有崩溃
- 绕开 gateway 的 Redis 去重，直接往队列里投两条 `message_id`完全相同的消息（模拟 RabbitMQ 至少一次投递的重复场景）→ 数据库里最终只有一行，验证了"队列至少一次投递 + 数据库唯一约束 = 业务只处理一次"这句话在真实场景下成立
- `GET /metrics`（worker 自己的 8001 端口）显示 `worker_messages_total` 按 `ok=2/forbidden=2/llm_degraded=1/dead_letter=2/duplicate=1` 精确对上这一整轮测试做的事，`process_seconds_count=8` 等于处理的消息总数，`first_token_seconds_count=2` 只在两次真正调用成功 LLM 时被记录（降级/越权/去重/死信都不会记）
- 验证完执行了 `docker compose down`

**人工审查与修复点**：
设计审查时发现幂等判断把"已接收"当成"已处理"，worker 中途崩溃会导致消息被跳过、用户收不到回复，已增加 status 字段区分。

**修复细节**：
- 问题场景：原设计里 worker 先 `INSERT` 用户消息（这时就已经"占用"了 `(tenant_id, message_id)` 这个唯一约束的名额），然后才调 LLM、写 assistant 回复、ack 队列消息。如果 worker 在插入用户消息之后、写完回复之前崩溃（进程被杀、容器重启等），消息不会被 ack，RabbitMQ 会重新投递；但重新投递时 `INSERT ... ON CONFLICT DO NOTHING` 会因为那一行已经存在而插入失败，被原逻辑直接判定成"已处理过的重复消息"而跳过——用户消息实际上从来没有被回复过，但永远不会再被处理了。
- 处理：
  - `messages` 表加了 `status` 字段（Postgres 原生 ENUM：`received`/`replied`），迁移见 `migrations/versions/202609231000_add_message_status.py`（`app/common/models.py` 同步加了 `MessageStatus`）
  - `app/worker/handler.py`：`_insert_user_message` 改名 `_upsert_user_message`，插入时带上 `status=received`；插入冲突时不再直接判定为重复，而是多查一次已有记录的 `status`——`replied` 才是真正的重复（返回 `"done"`），`received` 说明上次处理到一半崩了，要重新走一遍生成回复的流程（返回 `"retry"`）；新增 `_mark_user_message_replied`，在 assistant 回复真正写完（含降级回复）之后才把 `status` 改成 `replied`，这样"标记为 replied"和"用户拿到回复"永远是同一时刻
- 验证方法：把 mock-llm 的 `latency_ms` 调到 15000（拉长首 token 前的等待时间），发一条消息，在 worker 已经发起 LLM 调用、但还没收到任何 token（也就还没写回复）的时候执行 `docker compose kill -s SIGKILL worker` 模拟硬崩溃，随后 `docker compose start worker` 拉起一个全新的 worker 进程；同一个 WebSocket 连接全程没断开，等重新投递的消息被新 worker 处理完
  - worker 日志实测打出了 `消息之前处理到一半就中断了（用户消息已入库但未回复），重新生成回复`，命中了新加的 `retry` 分支，并且带着和第一次完全相同的 `message_id`
  - 客户端在原连接上收到了完整的 `reply_chunk` + `reply_end`，拼出来的回复内容正常（不是空的、也不是重复触发了两次降级）
  - 查数据库确认：这条用户消息的 `status` 是 `replied`，这个会话下的 assistant 回复只有 1 行（没有因为重投递而产生重复的助手消息）
  - 额外跑了一遍之前的回归用例（正常发消息全链路、同租户/跨租户越权、绕开 gateway 直接投递重复消息到数据库唯一约束层面）确认没有破坏原有行为

---

## 步骤 1.8：脚本与 make demo

**日期**：2026-09-23

**改动/新建模块**：
- `scripts/ws_client.py`：命令行 WebSocket 客户端，参数是 `--token`/`--conversation-id`/`--content`/`--message-id`（不传就自动生成），打印 ACK 耗时、首 token 耗时、完整回复耗时三项，`ack status=duplicate` 时直接提示不会再触发新回复
- `scripts/demo.sh`：生成 token → 发一条消息（展示三个耗时）→ 用同一个 `message_id` 重发（展示 duplicate）
- `Makefile`：`demo` 目标从占位的 `bash scripts/demo.sh`（跑在宿主机）改成 `docker compose run --rm tools sh scripts/demo.sh`（跑在 `tools` 一次性容器里），跟 `migrate`/`seed` 保持一致的执行方式——这样不用要求宿主机装 Python/`websockets`，而且默认能用 docker 网络内部地址连 gateway

**关键决策**：
- `ws_client.py` 默认连 `ws://gateway:8000/ws`（docker 网络内部地址），因为设计上就是要跑在 `tools` 容器里而不是宿主机；留了 `--gateway-url` 参数，需要从宿主机直接跑的话可以传 `ws://localhost:8000/ws`。
- `message_id` 是否复用交给调用方决定（`--message-id` 有没有传），`demo.sh` 靠脚本打印的 `MESSAGE_ID=xxx` 这一行机器可解析的输出拿到第一次用的 id，再传给第二次调用，不用自己造 uuid 生成/解析逻辑。

**验证记录**：
- 完整走了一遍 `make up → make migrate → make seed → make demo`：
  - token 正常生成
  - 第一条消息：`ACK accepted 耗时=30.6ms`、`首 token 耗时=491.1ms`、`完整回复耗时=3004.2ms`，三个耗时都打出来了，回复内容跟 mock-llm 模板一致
  - 重发同一个 `message_id`：`ACK duplicate`，没有触发新的回复生成
- 验证完执行了 `docker compose down`

**人工审查与修复点**：
（留空，由 Jo 填写）

---

## 步骤 1.9：README 初版

**日期**：2026-09-23

**改动模块**：
- `README.md`：项目简介、Mermaid 架构图、设计假设（照抄 PHASE1 原文，加了一句解释"为什么 gateway/worker 靠 MQ+Redis 解耦不直接调用"）、端口表（补全了备注，去掉了之前"步骤 X 加入"这种占位说明）、启动步骤、目录说明、Makefile 目标一览（含解释 `tools` 容器的 `profiles` 机制）

**关键决策**：
- 端口表和目录说明尽量对着实际跑出来的东西写，不是照搬 PHASE1 文档的字面描述——比如明确写了 mock-im/RabbitMQ 管理界面浏览器怎么打开、`make demo` 之外怎么手工拿 token。
- 没有为阶段一临时加自动化测试（`make test` 依然是占位），README 里也如实写清楚"阶段一暂无自动化测试"，不打没做过的事的埋伏笔。

**验证记录**：
- Mermaid 图语法用 mermaid.live 风格手工核对过节点和箭头方向，没有实际用工具渲染截图，若有语法问题请指出
- 端口表、启动步骤里给的每条命令都是这一路验证下来真实跑过的命令，不是凭空写的

**人工审查与修复点**：
（留空，由 Jo 填写）
