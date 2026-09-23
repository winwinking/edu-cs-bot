.PHONY: up down logs ps migrate seed demo test loadtest

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f

ps:
	docker compose ps

# gateway / worker / scheduler 共用一个镜像，迁移在 worker 容器里跑
migrate:
	docker compose run --rm worker alembic upgrade head

seed:
	docker compose run --rm worker python scripts/seed.py

demo:
	bash scripts/demo.sh

test:
	@echo "待实现"

loadtest:
	@echo "待实现"
