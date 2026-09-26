.PHONY: up down logs ps migrate seed reindex demo test loadtest

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f

ps:
	docker compose ps

# tools 是一次性任务容器（profiles 挡住了 make up 默认启动它），迁移/种子数据这类活跑完就退出
migrate:
	docker compose run --rm tools alembic upgrade head

seed:
	docker compose run --rm tools sh -c "python scripts/seed.py && python scripts/reindex.py"

reindex:
	docker compose run --rm tools python scripts/reindex.py

demo:
	docker compose run --rm tools sh scripts/demo.sh

# 前提：已经 make up（依赖真实 PostgreSQL/Redis/RabbitMQ/mock-*/gateway/worker/scheduler）。
# 依次跑 unit（带核心模块覆盖率报告）、mock-llm 自身规则测试、integration、e2e，任何一层失败
# 整体失败（make 逐行执行 shell 命令，某一行非 0 退出码就停，不会继续跑下一层）
test:
	docker compose run --rm tools pytest tests/unit -q \
		--cov=app.worker.graph.classify \
		--cov=app.common.permissions \
		--cov=app.gateway.message_handler \
		--cov=app.worker.handler \
		--cov=app.common.masking \
		--cov=app.common.reminder_rules \
		--cov=app.common.tools \
		--cov-report=term-missing
	docker compose run --rm mocks-tools pytest tests/unit/test_mock_llm_rules.py -q
	docker compose run --rm tools pytest tests/integration -v
	docker compose run --rm tools pytest tests/e2e -v

loadtest:
	@echo "待实现"
