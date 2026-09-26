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


@pytest.mark.asyncio
async def test_invalid_json_replies_with_error_and_does_not_publish():
    ws = _FakeWebSocket()
    exchange = _FakeExchange()

    await handle_inbound_message(
        ws, tenant_id="t_a", user_id="u_a_1001", raw_text="不是 JSON", exchange=exchange
    )

    assert ws.sent[-1]["code"] == "invalid_json"
    assert exchange.published == []


@pytest.mark.asyncio
async def test_message_failing_schema_validation_replies_with_error():
    ws = _FakeWebSocket()
    exchange = _FakeExchange()
    # 缺 content 字段，Pydantic 校验不通过
    raw_text = json.dumps({"type": "message", "message_id": "m1", "conversation_id": "c1"})

    await handle_inbound_message(ws, tenant_id="t_a", user_id="u_a_1001", raw_text=raw_text, exchange=exchange)

    assert ws.sent[-1]["code"] == "invalid_message"
    assert exchange.published == []


@pytest.mark.asyncio
async def test_publish_failure_rolls_back_dedup_key_so_retry_is_not_treated_as_duplicate(monkeypatch):
    # 幂等的另一面：投递失败时如果不把刚写的去重键删掉，客户端稍后用同一个 message_id 重试
    # 会被误判成"重复消息"而永远收不到处理，这段测的就是这个回滚
    async def fake_rate_limit(tenant_id, user_id):
        return True

    monkeypatch.setattr(message_handler_module, "check_user_and_tenant_rate_limit", fake_rate_limit)
    fake_redis = _FakeRedisClient()
    monkeypatch.setattr(message_handler_module, "redis_client", fake_redis)

    class _FailingExchange:
        async def publish(self, message, *, routing_key: str, timeout: float) -> None:
            raise RuntimeError("MQ 挂了")

    ws = _FakeWebSocket()

    await handle_inbound_message(
        ws, tenant_id="t_a", user_id="u_a_1001", raw_text=_raw_message(), exchange=_FailingExchange()
    )

    assert ws.sent[-1]["code"] == "mq_publish_failed"
    assert fake_redis.delete_calls == 1  # 去重键被回滚删除


@pytest.mark.asyncio
async def test_publish_failure_dedup_rollback_tolerates_redis_error(monkeypatch):
    # 回滚去重键这一步自己也可能因为 Redis 又恰好挂了而失败，不能让这个失败盖掉真正的错误回复
    async def fake_rate_limit(tenant_id, user_id):
        return True

    monkeypatch.setattr(message_handler_module, "check_user_and_tenant_rate_limit", fake_rate_limit)

    class _FlakyDeleteRedisClient(_FakeRedisClient):
        async def delete(self, key) -> None:
            self.delete_calls += 1
            raise RedisError("连不上")

    fake_redis = _FlakyDeleteRedisClient()
    monkeypatch.setattr(message_handler_module, "redis_client", fake_redis)

    class _FailingExchange:
        async def publish(self, message, *, routing_key: str, timeout: float) -> None:
            raise RuntimeError("MQ 挂了")

    ws = _FakeWebSocket()

    await handle_inbound_message(
        ws, tenant_id="t_a", user_id="u_a_1001", raw_text=_raw_message(), exchange=_FailingExchange()
    )

    assert ws.sent[-1]["code"] == "mq_publish_failed"
