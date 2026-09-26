FROM python:3.11-slim AS base

WORKDIR /app

# scripts/xxx.py 直接跑的时候 sys.path[0] 是 scripts/ 不是 /app，没有这个 import app.common 会失败
ENV PYTHONPATH=/app

# 先拷贝依赖清单再装依赖，代码变动不会让 pip install 重跑，加快构建
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY alembic.ini .
COPY migrations/ ./migrations/

# 阶段四 4.2：tests/ 不 COPY 进这一阶段——gateway/worker/scheduler 是要跑起来对外提供服务的
# 生产镜像，带着测试代码违反"测试代码不进生产镜像"的硬性规则。tools 是唯一需要跑 pytest 的
# 一次性容器，靠 docker-compose.yml 里的 volumes 挂载把 ./tests 挂进去，只在
# `docker compose run tools ...` 这一次性容器的生命周期内可见，不会进镜像 layer、也不会出现
# 在 gateway/worker/scheduler 实际跑起来的容器里
CMD ["python", "-m", "app.gateway.main"]

# ---------- tools 专用构建阶段 ----------
# 阶段四 4.2 审查发现：pytest/pytest-asyncio/pytest-cov 这类测试工具本身如果直接写进
# requirements.txt，会被上面 base 这一层装进去，gateway/worker/scheduler 的生产镜像也是
# base 构建出来的，等于测试工具混进了生产镜像。这里另开一个从 base 继续往上叠的阶段，只有
# 显式指定 --target=tools 才会构建到这一层；docker-compose.yml 里只有 tools 服务用
# `build.target: tools`，其余服务用 `build.target: base`，两边镜像名也不同
# （edu-cs-bot/app-tools:latest vs edu-cs-bot/app:latest），不会互相覆盖。
FROM base AS tools

# 阶段四检查点 F 审查发现：scripts/ 原来跟 app/ 一起 COPY 进上面的 base 阶段，导致
# gateway/worker/scheduler 的生产镜像里也带着一整套操作工具脚本——里面既有能用 JWT_SECRET
# 现场签发 token 的（gen_token.py/chat.py 等），也有能重置/修改数据、清空死信队列的
# （seed.py/reindex.py/dlq_replay.py/mockctl.py 等）。这些脚本只在 `docker compose run tools
# ...` 这种一次性排障/演示场景下才会被用到，gateway/worker/scheduler 自己的启动命令
# （`python -m app.xxx.main`）和 Makefile 里的 migrate/seed/reindex/demo 从来不会在
# gateway/worker/scheduler 容器里执行 scripts/ 下的任何文件，挪到只有 tools 才有的这个阶段
# 不影响任何现有调用方式。
COPY scripts/ ./scripts/

COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY pytest.ini .
