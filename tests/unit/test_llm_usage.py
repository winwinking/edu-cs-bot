"""覆盖 PHASE3.md 第 6 步 token 预算的验证清单（设计决定 13）：
- 机构自己设了 daily_token_budget 就用那个值，没设置才落回 .env 默认值。
- budget=None（不限额）永远放行；budget=0 从一开始（今天还没用过、Redis 里还没有这个键）
  就该判定超限，不能因为"没有历史用量"就放第一次调用过去。
- 用量达到/超过预算判定超限，没到就放行。
- Redis 报错时预算检查放行（设计决定 9，限流/去重/预算故障都不能把所有用户挡在外面）。

不连真实 Redis/数据库：`redis_client`/session 都换成本文件手写的假对象。
"""
import pytest
from redis.exceptions import RedisError

from app.common import llm_usage as llm_usage_module
from app.common.llm_usage import add_tokens_used, get_daily_budget, is_budget_exceeded


class _FakeTenant:
    def __init__(self, daily_token_budget):
        self.daily_token_budget = daily_token_budget


class _FakeSession:
    def __init__(self, tenant):
        self._tenant = tenant

    async def get(self, model, pk):
        return self._tenant


@pytest.mark.asyncio
async def test_get_daily_budget_uses_tenant_value_when_set():
    session = _FakeSession(_FakeTenant(500))
    assert await get_daily_budget(session, "t_a") == 500


@pytest.mark.asyncio
async def test_get_daily_budget_falls_back_to_default_when_tenant_budget_is_none(monkeypatch):
    monkeypatch.setattr(llm_usage_module.settings, "default_daily_token_budget", 999)
    session = _FakeSession(_FakeTenant(None))
    assert await get_daily_budget(session, "t_a") == 999


@pytest.mark.asyncio
async def test_no_budget_configured_never_exceeded():
    assert await is_budget_exceeded("t_a", "Asia/Shanghai", budget=None) is False


@pytest.mark.asyncio
async def test_zero_budget_exceeded_even_without_prior_usage(monkeypatch):
    async def fake_get(key):
        return None  # 今天还没调用过 LLM，Redis 里还没有这个键

    monkeypatch.setattr(llm_usage_module.redis_client, "get", fake_get)
    assert await is_budget_exceeded("t_a", "Asia/Shanghai", budget=0) is True


@pytest.mark.asyncio
async def test_usage_under_budget_is_not_exceeded(monkeypatch):
    async def fake_get(key):
        return "50"

    monkeypatch.setattr(llm_usage_module.redis_client, "get", fake_get)
    assert await is_budget_exceeded("t_a", "Asia/Shanghai", budget=100) is False


@pytest.mark.asyncio
async def test_usage_at_or_over_budget_is_exceeded(monkeypatch):
    async def fake_get(key):
        return "100"

    monkeypatch.setattr(llm_usage_module.redis_client, "get", fake_get)
    assert await is_budget_exceeded("t_a", "Asia/Shanghai", budget=100) is True


@pytest.mark.asyncio
async def test_redis_error_allows_through(monkeypatch):
    async def fake_get(key):
        raise RedisError("连不上")

    monkeypatch.setattr(llm_usage_module.redis_client, "get", fake_get)
    assert await is_budget_exceeded("t_a", "Asia/Shanghai", budget=100) is False


@pytest.mark.asyncio
async def test_add_tokens_used_skips_when_zero_or_negative(monkeypatch):
    called = False

    def fake_pipeline(**kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(llm_usage_module.redis_client, "pipeline", fake_pipeline)
    await add_tokens_used("t_a", "Asia/Shanghai", 0)
    assert called is False
