# edu-cs-bot

多租户教育平台 AI 客服机器人后端系统。

> 本文件是阶段一步骤 1.1 的占位版本，完整版（架构图、启动步骤、设计假设）将在步骤 1.9 补充。当前进度见 `docs/PHASE1.md` 和 `AGENT_LOG.md`。

## 端口（宿主机映射，均可在 `.env` 里改）

| 服务 | 宿主机端口 | 变量 | 备注 |
|---|---|---|---|
| PostgreSQL | 5432 | `POSTGRES_HOST_PORT` | |
| Redis | **6380** | `REDIS_HOST_PORT` | 特意没用默认的 6379——本机很可能已经跑着一个原生安装的 Redis 占用 6379，脚本连 `localhost:6379` 时会悄悄连到那个而不是容器里的这个，排查起来很晕，所以容器对外映射改成 6380（容器内部还是标准的 6379，服务间互联不受影响） |
| RabbitMQ | 5672 | `RABBITMQ_HOST_PORT` | |
| RabbitMQ 管理界面 | 15672 | `RABBITMQ_MANAGEMENT_HOST_PORT` | |
| gateway | 8000 | `GATEWAY_HOST_PORT` | 步骤 1.6 加入 |
| worker 健康检查 | 8001 | `WORKER_HEALTH_HOST_PORT` | 步骤 1.7 加入 |
| mock-im | 8080 | `MOCK_IM_HOST_PORT` | 步骤 1.5 加入 |
| mock-llm | 8100 | `MOCK_LLM_HOST_PORT` | 步骤 1.5 加入 |
| mock-knowledge | 8101 | `MOCK_KNOWLEDGE_HOST_PORT` | 步骤 1.5 加入 |
| mock-platform | 8102 | `MOCK_PLATFORM_HOST_PORT` | 步骤 1.5 加入 |
| mock-finance | 8103 | `MOCK_FINANCE_HOST_PORT` | 步骤 1.5 加入 |
