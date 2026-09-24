FROM python:3.11-slim

WORKDIR /app

# pytest 是独立的可执行入口（不是 python -m pytest），不会像 -m 运行模块那样自动把 cwd
# 加进 sys.path，要显式设置 PYTHONPATH 才能在跑单元测试时 import mocks.*（见 tests/unit/
# test_mock_llm_rules.py）
ENV PYTHONPATH=/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mocks/ ./mocks/
COPY tests/ ./tests/

# 各 mock 服务共用本镜像，实际启动哪个模块由 docker-compose 的 command 决定
CMD ["python", "-m", "mocks.mock_llm.main"]
