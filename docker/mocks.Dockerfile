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
# mock-im（阶段三第 7 步的演示控制台）要按开发用签发方式生成 token（app.common.auth）、
# 按 tenant_id+user_id 查真实角色（app.common.db/models），所以这个共用镜像也要带上 app/；
# 其余 4 个 mock 服务不引用这些模块，多带这些文件对它们没有影响
COPY app/ ./app/

# 各 mock 服务共用本镜像，实际启动哪个模块由 docker-compose 的 command 决定
CMD ["python", "-m", "mocks.mock_llm.main"]
