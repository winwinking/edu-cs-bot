FROM python:3.11-slim

WORKDIR /app

# 先拷贝依赖清单再装依赖，代码变动不会让 pip install 重跑，加快构建
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY alembic.ini .
COPY migrations/ ./migrations/
COPY scripts/ ./scripts/

# gateway / worker / scheduler 共用本镜像，实际启动哪个服务由 docker-compose 的 command 决定
CMD ["python", "-m", "app.gateway.main"]
