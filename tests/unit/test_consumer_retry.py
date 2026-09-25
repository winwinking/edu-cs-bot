"""覆盖 PHASE3.md 第 5 步死信的验证清单：x-retry-count 小于 DLQ_MAX_RETRIES 就重新投回原队列、
次数加 1；等于就进死信；消息体本身解析不出来直接进死信，不走重试。

不连真实 RabbitMQ：手写假的 IncomingMessage/Exchange 记录调用过什么，验证 `_on_message` 的分支
逻辑，真正连 RabbitMQ 的死信/重投链路走 phase3_smoke.py 的故障注入场景。
"""
import json

import pytest

from app.worker import consumer as consumer_module
from app.worker.consumer import _on_message


class _FakeMessage:
    def __init__(self, body: bytes, headers: dict | None = None):
        self.body = body
        self.headers = headers or {}
        self.content_type = "application/json"
        self.acked = False
        self.rejected_requeue: bool | None = None

    async def ack(self) -> None:
        self.acked = True

    async def reject(self, requeue: bool) -> None:
        self.rejected_requeue = requeue


class _FakeExchange:
    def __init__(self):
        self.published: list[dict] = []

    async def publish(self, message, *, routing_key: str, timeout: float) -> None:
        self.published.append({"headers": dict(message.headers), "routing_key": routing_key})


def _make_payload() -> bytes:
    return json.dumps(
        {
            "tenant_id": "t_a",
            "user_id": "u_a_1001",
            "conversation_id": "6f8f2c2e-1a4b-4e9a-9f1a-2c9a7e6d5b4a",
            "message_id": "m1",
            "content": "你好",
        }
    ).encode("utf-8")


@pytest.mark.asyncio
async def test_malformed_body_goes_straight_to_dead_letter_without_retry():
    message = _FakeMessage(b"not json", headers={"x-retry-count": 0})
    exchange = _FakeExchange()

    await _on_message(message, inbound_exchange=exchange)

    assert message.rejected_requeue is False
    assert exchange.published == []  # 没有走重试分支


@pytest.mark.asyncio
async def test_unexpected_exception_requeues_with_incremented_retry_count(monkeypatch):
    async def fake_process(**kwargs):
        raise RuntimeError("数据库连不上")

    monkeypatch.setattr(consumer_module, "process_inbound_message", fake_process)

    message = _FakeMessage(_make_payload(), headers={"x-retry-count": 1, "trace_id": "tr1", "tenant_id": "t_a"})
    exchange = _FakeExchange()

    await _on_message(message, inbound_exchange=exchange)

    assert message.acked is True  # 重新投回原队列后 ack 原消息，不是 reject
    assert message.rejected_requeue is None
    assert len(exchange.published) == 1
    assert exchange.published[0]["headers"]["x-retry-count"] == 2


@pytest.mark.asyncio
async def test_retry_count_at_threshold_goes_to_dead_letter(monkeypatch):
    async def fake_process(**kwargs):
        raise RuntimeError("数据库连不上")

    monkeypatch.setattr(consumer_module, "process_inbound_message", fake_process)
    monkeypatch.setattr(consumer_module.settings, "dlq_max_retries", 3)

    message = _FakeMessage(_make_payload(), headers={"x-retry-count": 3})
    exchange = _FakeExchange()

    await _on_message(message, inbound_exchange=exchange)

    assert message.rejected_requeue is False
    assert exchange.published == []


@pytest.mark.asyncio
async def test_missing_retry_header_defaults_to_zero(monkeypatch):
    async def fake_process(**kwargs):
        raise RuntimeError("意外异常")

    monkeypatch.setattr(consumer_module, "process_inbound_message", fake_process)

    message = _FakeMessage(_make_payload(), headers={})
    exchange = _FakeExchange()

    await _on_message(message, inbound_exchange=exchange)

    assert exchange.published[0]["headers"]["x-retry-count"] == 1


@pytest.mark.asyncio
async def test_successful_processing_just_acks(monkeypatch):
    async def fake_process(**kwargs):
        return "ok", "chitchat"

    monkeypatch.setattr(consumer_module, "process_inbound_message", fake_process)

    message = _FakeMessage(_make_payload(), headers={})
    exchange = _FakeExchange()

    await _on_message(message, inbound_exchange=exchange)

    assert message.acked is True
    assert exchange.published == []
