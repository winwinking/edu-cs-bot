# CLAUDE.md —— 项目长期规则（每次会话必读）

## 项目是什么
多租户教育平台 AI 客服机器人后端系统。完整需求见 `docs/REQUIREMENTS.md`（面试实战题原文）。
当前阶段的任务见 `docs/PHASE*.md`，只做当前阶段的内容，不要提前实现后续阶段。

我（Jo）是这个项目的负责人，需要在视频讲解中解释每一个设计决策、现场改需求、现场排障。
所以：**你写的每一处关键代码，我都必须能看懂、能讲出为什么这样写。**

## 架构总览
- `gateway`：WebSocket 接入层。鉴权、消息校验、去重、投递队列、ACK、回推回复。不调用 LLM。
- `worker`：消费队列，处理业务（意图路由、检索、工具调用、生成回复），可水平扩展多实例。
- `scheduler`：提醒调度（后续阶段）。
- `mocks/`：mock-im、mock-llm、mock-knowledge、mock-platform、mock-finance。
- 基础设施：PostgreSQL 15 + pgvector、Redis 7、RabbitMQ。
- gateway / worker / scheduler 共用一个镜像，靠不同启动命令区分；mocks 共用另一个镜像。

## 技术栈
Python 3.11（容器内）、FastAPI、Uvicorn、Pydantic v2、pydantic-settings、SQLAlchemy 2.x（async）+ asyncpg、Alembic、redis-py（asyncio）、aio-pika、openai SDK（AsyncOpenAI，通过 base_url 指向 mock-llm 或 DeepSeek）、structlog、prometheus-client、PyJWT、httpx、pytest + pytest-asyncio、LangGraph（阶段二起用于 worker 的业务编排）。

## 硬性规则（违反任何一条都是面试淘汰项）
1. **密钥零硬编码**：所有配置（数据库密码、JWT 密钥、API Key）只从环境变量读，统一走 `app/common/config.py` 的 Settings。`.env` 必须在 `.gitignore` 里，只提交 `.env.example`。
2. **日志脱敏**：日志永远不打印完整 token、密码、身份证、银行卡、手机号、邮箱。WebSocket 的 query string 含 token，不得原样记录。
3. **租户隔离**：所有业务表都有 `tenant_id`，所有业务查询必须带 `tenant_id` 过滤条件。
4. **全异步**：async 函数里禁止阻塞调用（禁止 `requests`、`time.sleep`、同步数据库驱动）。
5. **所有外部调用必须有超时**（LLM、mock 服务、数据库、Redis）。
6. **禁止无限重试**：重试必须有次数上限和退避间隔。
7. **SQL 只用参数化**（SQLAlchemy 表达式或绑定参数），禁止字符串拼接 SQL。
8. **用户输入不得拼进 system prompt**：用户内容只能放在 user 角色消息里。
9. **LLM 输出不得直接执行**：工具调用参数必须经 Pydantic/JSON Schema 校验 + 权限校验后才能执行（阶段二起）。

## 代码规范
- 全部加类型注解；所有消息体、请求体用 Pydantic 模型定义。
- 关键设计决策处写简短中文注释，说明**为什么**这样做（例如"先确认队列收到再 ACK，保证不丢消息"），不要写"这里定义一个变量"这种废话注释。
- 结构化日志统一用 structlog，每条日志自动带 `trace_id`、`tenant_id`、`conversation_id`（通过 contextvars 绑定）。
- 所有 shell 脚本、Dockerfile 使用 LF 换行。
- 开发环境是 Windows + Docker Desktop，但所有服务运行在 Linux 容器内。Makefile 和脚本必须在评审方的 Linux/macOS 上也能直接运行。

## 工作方式
1. 按 PHASE 文档里的小步骤顺序实施，**每完成一个小步骤就停下来**，告诉我：
   - 改了/新建了哪些文件
   - 关键设计点（2~4 条，用大白话说清楚为什么）
   - 我该运行什么命令来验证
   - 建议的 commit message
2. 等我验证通过、说"继续"之后，再做下一步。不要自己 git commit，由我验证后提交。
3. 需求不明确时先问我，不要自己猜着扩展范围。
4. 如果你偏离了 PHASE 文档的设计，必须明确告诉我偏离了什么、为什么。
5. 每个小步骤完成时，在 `AGENT_LOG.md` 末尾追加一条记录：步骤名、你生成/修改的模块、关键决策。"人工审查与修复点"不要留空占位，由我口述事实，你按事实整理填写，不得自行补充。

