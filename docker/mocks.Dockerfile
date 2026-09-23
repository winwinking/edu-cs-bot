FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mocks/ ./mocks/

# 各 mock 服务共用本镜像，实际启动哪个模块由 docker-compose 的 command 决定
CMD ["python", "-m", "mocks.mock_llm.main"]
