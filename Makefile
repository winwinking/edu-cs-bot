.PHONY: up down logs ps migrate seed demo test loadtest

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
	docker compose run --rm tools python scripts/seed.py

demo:
	docker compose run --rm tools sh scripts/demo.sh

test:
	@echo "待实现"

loadtest:
	@echo "待实现"
