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
（留空，由 Jo 填写）
