.PHONY: up down logs ps migrate seed reindex demo test loadtest loadtest-users loadtest-steady loadtest-burst loadtest-finance loadtest-llm-timeout eval

# 一键启动：等全部容器 healthy 后自动跑迁移 + 种子数据，评委不需要再手动敲 make migrate/
# make seed 就能直接 make test/make demo。migrate 和 seed 都是幂等的（alembic 本身幂等；
# seed.py 里 tenants 现在也用 ON CONFLICT DO NOTHING，不会把压测/演示中改过的
# daily_token_budget 等字段冲回种子默认值），重复执行 make up 不会产生重复数据。
#
# RabbitMQ 健康检查（rabbitmq-diagnostics ping）通过的那一刻，AMQP 端口不一定已经能接受新
# 连接——worker 偶尔会在这个窄窗口第一次连接失败退出；`restart: unless-stopped` 会在几秒内
# 自己拉起来重连成功，这是已有的自愈机制，不是新引入的问题，只是以前 make up 不等 healthy
# 状态，没人注意到这个瞬时抖动。下面用 --wait 确认全部 healthy 再往下走（保证 migrate/seed
# 执行时所有服务都真的活着），失败了重试一次而不是直接判失败，给自愈留出时间。重试不用
# `sleep`——`sleep` 不是 cmd.exe 的内置命令，之前 `sleep 10 && ...` 这个写法在原生 cmd.exe
# 下报 "'sleep' is not recognized"，`up` 直接中断（阶段五 5.7 复核发现，见 AGENT_LOG.md
# 索引"agent 自查修复"）；`docker compose up -d --wait` 本身就会反复轮询直到全部 healthy
# 或者等到它自己的等待超时才返回，不需要外部再睡一段时间才重试，直接再跑一次同一条命令，
# 让它自己重新走一遍轮询等待即可——`||` 是 cmd.exe/Git Bash/POSIX sh 共同支持的写法，不用
# 再分平台各写一套。
#
# tools 服务不在 `up` 启动的服务集合里（profiles 挡住了），上面的 --build 不会重新构建它；
# `docker compose run` 默认只在镜像不存在时才现场构建，已经存在的旧镜像不会自动刷新——
# 这里必须显式加 --build，否则改过 scripts/seed.py 之类的代码后 make up 会悄悄拿旧镜像跑
# migrate/seed，改动不生效（原始问题就是这样被发现的：seed.py 改成 ON CONFLICT DO NOTHING
# 之后，第一次验证 make up 因为这里没加 --build，实际跑的还是旧镜像里的 DO UPDATE，把手动
# 改过的 daily_token_budget 冲回了种子默认值）
#
# 注意（阶段五 5.7 审查发现）：这个目标的配方里不能出现以 Tab 开头的 # 注释行——Linux/macOS/
# Git Bash 下 make 把配方交给 sh 执行，sh 把 # 开头的行当注释跳过；Windows cmd 下 make 没有
# sh 可用，会把这类行原样当命令执行，导致 "CreateProcess(NULL, # ...) failed" 报错，
# up 在对应行中断。所有说明性注释统一写在这里（目标定义上方，不带 Tab），配方本身只保留
# 命令行（见 AGENT_LOG.md 索引第 44 条）。
up:
	docker compose up -d --build
	docker compose up -d --wait || docker compose up -d --wait
	docker compose run --rm --build tools alembic upgrade head
	docker compose run --rm --build tools sh -c "python scripts/seed.py && python scripts/reindex.py"

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
#
# PHASE4.md 4.6 审查修复：mock-llm 新增 timeout_rate 之后多了一个测这个新参数的文件
# （test_mock_llm_timeout.py），跟原来的 test_mock_llm_rules.py 一样只能在 mocks 镜像里
# 跑，用文件名列表而不是原来那样单写一个文件名，之后 mock-llm 再加测试文件也在这里加一个
# （注意：配方里不能再放 Tab 开头的 # 注释行，理由同 up 目标，见 AGENT_LOG.md 索引第 44 条）
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
	docker compose run --rm mocks-tools pytest tests/unit/test_mock_llm_rules.py tests/unit/test_mock_llm_timeout.py -q
	docker compose run --rm tools pytest tests/integration -v
	docker compose run --rm tools pytest tests/e2e -v

# 阶段四 4.6。前提：已经 make up。
# k6 服务带 profiles: ["loadtest"]，`docker compose run` 对一次性容器有效，不需要额外加
# --profile 参数就能跑起来（profiles 只挡 up/start 这种"常驻启动"，不挡 run）。
#
# 5.1 全新克隆审查发现：`make loadtest` 依赖 loadtest/tokens.json（.gitignore 挡掉，不进
# git），评委不知道要先手动跑 loadtest-users 的话，k6 会直接抛 "stat tokens.json: no such
# file" 的原始堆栈。改成下面四个场景各自依赖 loadtest-users，评委敲任何一个目标（包括聚合的
# `make loadtest`）都会自动先备好 token，不需要知道这一步的存在。选择"每次都重新生成"而不是
# "只在文件不存在时生成"：gen_users.py 对用户本身是幂等的（ON CONFLICT DO NOTHING，重跑不
# 产生重复行、1600 个用户批量 upsert 也就一两秒），但 token 有 6 小时有效期，如果只在文件缺失
# 时生成，环境跑了一整天之后再跑压测会拿到一批已过期的 token，报出来的是一堆认证失败，会被
# 误判成系统故障而不是"token 过期"这种配置问题；每次都重新生成能保证拿到的 token 一定在有效期
# 内，多花的一两秒可以忽略。make target 是同一次 make 调用里只会真正执行一次的 phony
# 目标——`make loadtest` 依次跑四个场景，loadtest-users 只会在第一次用到时执行一遍，不会跑四次。
loadtest-users:
	docker compose run --rm tools python loadtest/gen_users.py --tenant t_a --count 1600

loadtest-steady: loadtest-users
	mkdir -p loadtest/output
	docker compose run --rm k6 run --out csv=/loadtest/output/steady.csv steady.js

loadtest-burst: loadtest-users
	mkdir -p loadtest/output
	docker compose run --rm k6 run --out csv=/loadtest/output/burst.csv burst.js

loadtest-finance: loadtest-users
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
loadtest-llm-timeout: loadtest-users
	mkdir -p loadtest/output
	sh -c 'trap "docker compose run --rm tools python scripts/mockctl.py llm reset" EXIT; \
		docker compose run --rm tools python scripts/mockctl.py llm timeout_rate=0.2 && \
		docker compose run --rm k6 run --out csv=/loadtest/output/llm_timeout.csv llm_timeout.js'

# 四个场景挨个跑，也能用上面单独的目标只跑一个（PHASE4.md 4.6 原文要求）
loadtest: loadtest-steady loadtest-burst loadtest-finance loadtest-llm-timeout

# 阶段五 5.4。前提：已经 make up，且两家机构今日 LLM token 预算没有用完（eval/run_eval.py
# 自己会在开跑前检查一遍，不满足会直接停止并提示，不会跑到一半才发现）。先跑打分函数的单测
# （不连数据库，纯规则），再跑真正的评测（真实连 gateway，50 道题挨个发一遍）。评测代码不进
# gateway/worker 的生产镜像，跟 4.2 处理测试代码是同一个思路：eval/ 只挂载进 tools 容器。
#
# 不在这里用 mkdir -p 建 eval/output 目录（阶段五 5.7 cmd.exe 验证发现 -p 是 POSIX 参数，
# cmd.exe 自带的 mkdir 不认识，会把它当成目录名创建出来）：eval/run_eval.py 的 main() 自己
# 在写任何文件之前会先 `OUTPUT_DIR.mkdir(exist_ok=True)`（eval/output 的父目录 eval/ 本身
# 靠 docker-compose.yml 的 bind mount 保证一直存在），不需要 Makefile 这层再建一次，改成
# 在 Python 里建目录后天然跨平台，不用分平台写两套命令。
eval:
	docker compose run --rm tools pytest tests/unit/test_eval_scoring.py -q
	docker compose run --rm tools python eval/run_eval.py
