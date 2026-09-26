FROM python:3.11-slim AS base

WORKDIR /app

# pytest 是独立的可执行入口（不是 python -m pytest），不会像 -m 运行模块那样自动把 cwd
# 加进 sys.path，要显式设置 PYTHONPATH 才能在跑单元测试时 import mocks.*（见 tests/unit/
# test_mock_llm_rules.py）
ENV PYTHONPATH=/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mocks/ ./mocks/
# mock-im（阶段三第 7 步的演示控制台）要按开发用签发方式生成 token（app.common.auth）、
# 按 tenant_id+user_id 查真实角色（app.common.db/models），所以这个共用镜像也要带上 app/；
# 其余 4 个 mock 服务不引用这些模块，多带这些文件对它们没有影响
COPY app/ ./app/

# 阶段四 4.2：tests/ 不 COPY 进这一阶段，理由和 app.Dockerfile 一样。
CMD ["python", "-m", "mocks.mock_llm.main"]

# ---------- tools 专用构建阶段 ----------
# tests/unit/test_mock_llm_rules.py 要 import mocks.mock_llm.rules，只能在这个镜像里跑；
# 又不能把 pytest 装进上面 base 这一层（那样 mock-llm/mock-im 等常驻服务的生产镜像也会带上
# 测试工具，见 app.Dockerfile 同名阶段的注释），所以另开一个从 base 继续往上叠的阶段，只有
# docker-compose.yml 里的 mocks-tools 服务（build.target: tools，独立镜像名
# edu-cs-bot/mocks-tools:latest）会构建到这一层，5 个常驻 mock 服务用 build.target: base。
FROM base AS tools

COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY pytest.ini .
