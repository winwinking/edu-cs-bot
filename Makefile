.PHONY: up down logs ps migrate seed reindex demo test loadtest loadtest-users loadtest-steady loadtest-burst loadtest-finance loadtest-llm-timeout

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
# 依次跑 unit（带核心模块覆盖率报告）、mock-llm 自身测试、integration、e2e，任何一层失败
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
	# PHASE4.md 4.6 审查修复：mock-llm 新增 timeout_rate 之后多了一个测这个新参数的文件
	# （test_mock_llm_timeout.py），跟原来的 test_mock_llm_rules.py 一样只能在 mocks 镜像里
	# 跑，用文件名列表而不是原来那样单写一个文件名，之后 mock-llm 再加测试文件也在这里加一个
	docker compose run --rm mocks-tools pytest tests/unit/test_mock_llm_rules.py tests/unit/test_mock_llm_timeout.py -q
	docker compose run --rm tools pytest tests/integration -v
	docker compose run --rm tools pytest tests/e2e -v

# 阶段四 4.6。前提：已经 make up，并且已经跑过一次 loadtest-users（生成压测用户+token，
# 这一步不是每次压测都要重跑，token 有效期 6 小时，见 loadtest/gen_users.py）。
# k6 服务带 profiles: ["loadtest"]，`docker compose run` 对一次性容器有效，不需要额外加
# --profile 参数就能跑起来（profiles 只挡 up/start 这种"常驻启动"，不挡 run）。
loadtest-users:
	docker compose run --rm tools python loadtest/gen_users.py --tenant t_a --count 1600

loadtest-steady:
	mkdir -p loadtest/output
	docker compose run --rm k6 run --out csv=/loadtest/output/steady.csv steady.js

loadtest-burst:
	mkdir -p loadtest/output
	docker compose run --rm k6 run --out csv=/loadtest/output/burst.csv burst.js

loadtest-finance:
	mkdir -p loadtest/output
	docker compose run --rm k6 run --out csv=/loadtest/output/finance.csv finance.js

# LLM 超时场景：mockctl 设置和 k6 运行、reset 写在同一个 shell 进程里用 trap 兜底
# （PHASE4.md 4.6 检查点审查发现，见 AGENT_LOG）：一开始试过在 k6 那一行后面加 `|| true`，
# 只能挡住"k6 正常运行完但返回非零退出码"这一种情况；如果是跑到一半手动 Ctrl+C 中断，
# make 会直接终止整个目标，根本不会走到下面 reset 那一行，20% 超时率会一直留在 mock-llm 里，
# 污染后面接着跑的其它场景或者演示。trap EXIT 保证这个 shell 不管是正常跑完、
# docker compose run 返回非零、还是被 Ctrl+C 中断退出，都会执行一次 reset（trap 挡不住的只有
# kill -9 或者 Docker daemon 自己崩溃这种连 shell 自己都来不及处理信号的情况，这属于没有任何
# 应用层手段能兜底的极端场景，不在这里try 解决）
loadtest-llm-timeout:
	mkdir -p loadtest/output
	sh -c 'trap "docker compose run --rm tools python scripts/mockctl.py llm reset" EXIT; \
		docker compose run --rm tools python scripts/mockctl.py llm timeout_rate=0.2 && \
		docker compose run --rm k6 run --out csv=/loadtest/output/llm_timeout.csv llm_timeout.js'

# 四个场景挨个跑，也能用上面单独的目标只跑一个（PHASE4.md 4.6 原文要求）
loadtest: loadtest-steady loadtest-burst loadtest-finance loadtest-llm-timeout
