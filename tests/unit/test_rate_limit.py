"""覆盖 PHASE3.md 第 4 步限流的验证清单：超过用户上限被拒；Redis 报错时放行（设计决定 9）。

不连真实 Redis：`check_rate_limit` 内部调的是 `_incr_with_expire`（Lua 脚本的可调用对象），
直接 monkeypatch 这一个函数，模拟"计数器涨到几"和"Redis 报错"两种情况，比起真连 Redis 更快
也更稳定（不用等真实的 10 秒/1 秒窗口过期）。
"""
import pytest
from redis.exceptions import RedisError

from app.common import rate_limit as rate_limit_module
from app.common.rate_limit import check_rate_limit, check_user_and_tenant_rate_limit


@pytest.mark.asyncio
async def test_allows_when_under_limit(monkeypatch):
    async def fake_incr(keys, args):
        return 5

    monkeypatch.setattr(rate_limit_module, "_incr_with_expire", fake_incr)
    assert await check_rate_limit("k", limit=20, window_seconds=10) is True


@pytest.mark.asyncio
async def test_rejects_when_over_limit(monkeypatch):
    async def fake_incr(keys, args):
        return 21

    monkeypatch.setattr(rate_limit_module, "_incr_with_expire", fake_incr)
    assert await check_rate_limit("k", limit=20, window_seconds=10) is False


@pytest.mark.asyncio
async def test_exactly_at_limit_is_allowed(monkeypatch):
    """第 20 条本身应该放行，从第 21 条才算超限——INCR 之后如果 <= limit 就还在限额内。"""

    async def fake_incr(keys, args):
        return 20

    monkeypatch.setattr(rate_limit_module, "_incr_with_expire", fake_incr)
    assert await check_rate_limit("k", limit=20, window_seconds=10) is True


@pytest.mark.asyncio
async def test_redis_error_allows_through(monkeypatch):
    """限流组件自己出故障不能把所有用户都挡在外面（设计决定 9）。"""

    async def fake_incr(keys, args):
        raise RedisError("连不上")

    monkeypatch.setattr(rate_limit_module, "_incr_with_expire", fake_incr)
    assert await check_rate_limit("k", limit=20, window_seconds=10) is True


@pytest.mark.asyncio
async def test_tenant_limit_also_checked(monkeypatch):
    """用户维度没超，但机构维度超了，也要算超限——两个 key 都要查，任意一个超就算超。"""

    async def fake_incr(keys, args):
        if keys[0].startswith("ratelimit:user:"):
            return 1
        return 99999

    monkeypatch.setattr(rate_limit_module, "_incr_with_expire", fake_incr)
    allowed = await check_user_and_tenant_rate_limit("t_a", "u_a_1001")
    assert allowed is False
