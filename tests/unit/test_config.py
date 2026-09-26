"""覆盖阶段四审查发现的真实事故：数字类配置项在环境变量里是空字符串（`KEY=`，等号后面
不填）时，Settings() 不应该报错，应该落回代码默认值。

真实起因：`.env.example` 里 `DEFAULT_DAILY_TOKEN_BUDGET=`（注释写"留空就是不限额"）被
`cp .env.example .env` 原样复制成 CI 的正式配置，pydantic 把这个空字符串当成"传了一个值"去
解析成 `Optional[int]`，解析失败让 `Settings()` 在模块导入阶段直接抛异常——本机之前也发生过
一次一样的事故（手动把这一项删空，mock-im/worker 反复重启）。修复是给 `Settings.model_config`
加 `env_ignore_empty=True`（见 app/common/config.py），这里补单元测试锁住这个行为，不用再靠
真的跑一遍 CI 才能发现回归。

不连真实数据库/Redis：只是构造 `Settings` 对象，用 `monkeypatch.setenv` 喂一套满足必填字段的
最小环境变量，`_env_file=None` 跳过读 `.env` 文件本身（测试环境不应该依赖某个具体的 .env 文件
是否存在）。
"""
import pytest

from app.common.config import Settings

_REQUIRED_ENV = {
    "POSTGRES_USER": "u",
    "POSTGRES_PASSWORD": "p",
    "POSTGRES_DB": "d",
    "POSTGRES_HOST": "postgres",
    "DATABASE_URL": "postgresql+asyncpg://u:p@postgres/d",
    "REDIS_HOST": "redis",
    "REDIS_URL": "redis://redis:6379/0",
    "RABBITMQ_USER": "guest",
    "RABBITMQ_PASSWORD": "p",
    "RABBITMQ_HOST": "rabbitmq",
    "RABBITMQ_URL": "amqp://guest:p@rabbitmq/",
    "JWT_SECRET": "s",
    "LLM_BASE_URL": "http://mock-llm:8000/v1",
    "LLM_API_KEY": "k",
    "LLM_MODEL": "m",
    "FINANCE_SERVICE_TOKEN": "t",
}


@pytest.fixture
def _minimal_required_env(monkeypatch):
    for key, value in _REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)


def test_empty_string_env_var_falls_back_to_default_for_optional_int(monkeypatch, _minimal_required_env):
    # 复现事故本身：DEFAULT_DAILY_TOKEN_BUDGET= （空字符串），不应该抛 ValidationError
    monkeypatch.setenv("DEFAULT_DAILY_TOKEN_BUDGET", "")

    settings = Settings(_env_file=None)

    assert settings.default_daily_token_budget is None  # 落回代码默认值：不限额


def test_empty_string_env_var_falls_back_to_default_for_str_with_default(monkeypatch, _minimal_required_env):
    # REDIS_PASSWORD 的默认值本来就是空字符串，这里确认"当成没设置"不会把这类字段变成别的东西
    monkeypatch.setenv("REDIS_PASSWORD", "")

    settings = Settings(_env_file=None)

    assert settings.redis_password == ""


def test_non_empty_int_env_var_still_parses_normally(monkeypatch, _minimal_required_env):
    # 回归检查：env_ignore_empty 不应该影响正常传值的情况
    monkeypatch.setenv("DEFAULT_DAILY_TOKEN_BUDGET", "500000")

    settings = Settings(_env_file=None)

    assert settings.default_daily_token_budget == 500000


def test_required_string_field_left_empty_now_fails_fast(monkeypatch, _minimal_required_env):
    # 必填字段（没有默认值）如果被留空，以前会静默变成空字符串密钥，现在 env_ignore_empty
    # 会把它当成"没传"，pydantic 因为缺少必填字段直接报错——这是刻意变严格，不是意外副作用
    monkeypatch.setenv("JWT_SECRET", "")

    with pytest.raises(Exception):
        Settings(_env_file=None)
