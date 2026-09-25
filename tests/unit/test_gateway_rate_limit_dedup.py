"""覆盖 PHASE3.md 第 4 步限流+去重顺序的验证清单（gateway 这一侧）：
- 被限流的消息不写去重键、不投递 MQ，ack 状态是 rate_limited 并带提示文案。
- 去重检查本身如果 Redis 报错，跳过去重继续投递（设计决定 9），不能让整条消息处理失败。

不连真实 Redis/RabbitMQ：手写假的 redis_client/exchange，直接调用 handle_inbound_message。
"""
import json

import pytest
from redis.exceptions import RedisError

from app.gateway import message_handler as message_handler_module
from app.gateway.message_handler import handle_inbound_message


class _FakeWebSocket:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_text(self, data: str) -> None:
        self.sent.append(json.loads(data))


class _FakeExchange:
    def __init__(self):
        self.published: list[dict] = []

    async def publish(self, message, *, routing_key: str, timeout: float) -> None:
        self.published.append({"routing_key": routing_key})


class _FakeRedisClient:
    def __init__(self, *, set_result=True, raise_on_set: bool = False):
        self._set_result = set_result
        self._raise_on_set = raise_on_set
        self.set_calls = 0
        self.delete_calls = 0

    async def set(self, key, value, *, nx, ex):
        self.set_calls += 1
        if self._raise_on_set:
            raise RedisError("连不上")
        return self._set_result

    async def delete(self, key) -> None:
        self.delete_calls += 1


def _raw_message(content: str = "你好") -> str:
    return json.dumps(
        {"type": "message", "message_id": "m1", "conversation_id": "c1", "content": content}
    )


@pytest.mark.asyncio
async def test_rate_limited_message_is_not_deduped_or_published(monkeypatch):
    async def fake_rate_limit(tenant_id, user_id):
        return False

    monkeypatch.setattr(message_handler_module, "check_user_and_tenant_rate_limit", fake_rate_limit)
    fake_redis = _FakeRedisClient()
    monkeypatch.setattr(message_handler_module, "redis_client", fake_redis)

    ws = _FakeWebSocket()
    exchange = _FakeExchange()

    await handle_inbound_message(ws, tenant_id="t_a", user_id="u_a_1001", raw_text=_raw_message(), exchange=exchange)

    assert ws.sent[-1]["status"] == "rate_limited"
    assert ws.sent[-1]["detail"] == message_handler_module.RATE_LIMITED_REPLY
    assert fake_redis.set_calls == 0  # 没写去重键
    assert exchange.published == []  # 没投递 MQ


@pytest.mark.asyncio
async def test_dedup_redis_error_skips_dedup_and_still_publishes(monkeypatch):
    async def fake_rate_limit(tenant_id, user_id):
        return True

    monkeypatch.setattr(message_handler_module, "check_user_and_tenant_rate_limit", fake_rate_limit)
    fake_redis = _FakeRedisClient(raise_on_set=True)
    monkeypatch.setattr(message_handler_module, "redis_client", fake_redis)

    ws = _FakeWebSocket()
    exchange = _FakeExchange()

    await handle_inbound_message(ws, tenant_id="t_a", user_id="u_a_1001", raw_text=_raw_message(), exchange=exchange)

    assert ws.sent[-1]["status"] == "accepted"  # 跳过去重，继续按新消息处理
    assert len(exchange.published) == 1


@pytest.mark.asyncio
async def test_duplicate_message_still_detected_when_redis_healthy(monkeypatch):
    async def fake_rate_limit(tenant_id, user_id):
        return True

    monkeypatch.setattr(message_handler_module, "check_user_and_tenant_rate_limit", fake_rate_limit)
    fake_redis = _FakeRedisClient(set_result=False)  # SET NX 没抢到，说明是重复
    monkeypatch.setattr(message_handler_module, "redis_client", fake_redis)

    ws = _FakeWebSocket()
    exchange = _FakeExchange()

    await handle_inbound_message(ws, tenant_id="t_a", user_id="u_a_1001", raw_text=_raw_message(), exchange=exchange)

    assert ws.sent[-1]["status"] == "duplicate"
    assert exchange.published == []
