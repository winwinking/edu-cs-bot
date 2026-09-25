"""单条入站消息的处理流程：校验 -> 限流 -> 去重 -> 投递 MQ -> 回 ack/error。

顺序按设计决定 8：限流放在去重之前——被限流的消息不写去重键，用户稍后用同一个 message_id
重发仍然能被正常接收，不会被误判成"重复消息"。
"""
import json
import time
import uuid
from typing import Optional

import aio_pika
from aio_pika.abc import AbstractExchange
from fastapi import WebSocket
from pydantic import ValidationError
from redis.exceptions import RedisError

from app.common.config import get_settings
from app.common.logging import bind_trace_context, get_logger
from app.common.mq import INBOUND_ROUTING_KEY
from app.common.rate_limit import check_user_and_tenant_rate_limit
from app.common.redis import note_redis_result, redis_client
from app.common.schemas import AckMessage, ClientMessage, ErrorMessage

from app.gateway.metrics import ack_latency_seconds, inbound_messages_total

logger = get_logger(__name__)
settings = get_settings()

RATE_LIMITED_REPLY = "发得有点快，稍等几秒再发。"


async def _send_error(websocket: WebSocket, message_id: Optional[str], code: str, detail: str) -> None:
    err = ErrorMessage(message_id=message_id, code=code, detail=detail)
    await websocket.send_text(err.model_dump_json())


async def _send_ack(
    websocket: WebSocket, message_id: str, status: str, trace_id: str, *, detail: Optional[str] = None
) -> None:
    ack = AckMessage(message_id=message_id, status=status, trace_id=trace_id, detail=detail)
    await websocket.send_text(ack.model_dump_json())


async def _publish_inbound(
    exchange: AbstractExchange,
    *,
    tenant_id: str,
    user_id: str,
    msg: ClientMessage,
    trace_id: str,
) -> None:
    body = json.dumps(
        {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "conversation_id": msg.conversation_id,
            "message_id": msg.message_id,
            "content": msg.content,
            "trace_id": trace_id,
        },
        ensure_ascii=False,
    ).encode("utf-8")

    message = aio_pika.Message(
        body=body,
        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        content_type="application/json",
        headers={"trace_id": trace_id, "tenant_id": tenant_id, "user_id": user_id},
    )
    # publisher confirms 已经在 channel 上开了，这个 publish 会等 broker 确认收到才返回；
    # 复用连接超时的值当 publish 超时，没必要为每个 MQ 操作都单独开一个配置项
    await exchange.publish(
        message,
        routing_key=INBOUND_ROUTING_KEY,
        timeout=settings.mq_connect_timeout_seconds,
    )


async def handle_inbound_message(
    websocket: WebSocket,
    *,
    tenant_id: str,
    user_id: str,
    raw_text: str,
    exchange: AbstractExchange,
) -> None:
    trace_id = uuid.uuid4().hex
    start = time.monotonic()

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError:
        bind_trace_context(trace_id=trace_id, tenant_id=tenant_id)
        await _send_error(websocket, None, "invalid_json", "消息不是合法的 JSON")
        inbound_messages_total.labels(tenant_id=tenant_id, status="error").inc()
        logger.warning("收到非 JSON 消息")
        return

    try:
        msg = ClientMessage.model_validate(data)
    except ValidationError as exc:
        message_id = data.get("message_id") if isinstance(data, dict) else None
        bind_trace_context(trace_id=trace_id, tenant_id=tenant_id)
        await _send_error(websocket, message_id, "invalid_message", str(exc))
        inbound_messages_total.labels(tenant_id=tenant_id, status="error").inc()
        logger.warning("消息校验失败", errors=exc.errors())
        return

    bind_trace_context(trace_id=trace_id, tenant_id=tenant_id, conversation_id=msg.conversation_id)

    allowed = await check_user_and_tenant_rate_limit(tenant_id, user_id)
    if not allowed:
        await _send_ack(websocket, msg.message_id, "rate_limited", trace_id, detail=RATE_LIMITED_REPLY)
        inbound_messages_total.labels(tenant_id=tenant_id, status="rate_limited").inc()
        logger.info("消息被限流", message_id=msg.message_id)
        return

    # SET NX EX：原子地"不存在就写入并设过期"，天然避免并发重复消息的竞态。
    # Redis 报错时跳过去重这一层，继续往下投递（设计决定 9）：真正的重复兜底交给 worker 那边
    # (tenant_id, message_id) 唯一约束，只是那道防线要等消息真正入库才生效，不如 Redis 这层快
    dedup_key = f"dedup:{tenant_id}:{msg.message_id}"
    try:
        is_new = await redis_client.set(dedup_key, "1", nx=True, ex=settings.dedup_ttl_seconds)
        note_redis_result(True)
    except RedisError as exc:
        note_redis_result(False)
        logger.warning("去重检查调用 Redis 失败，跳过去重", message_id=msg.message_id, error=str(exc))
        is_new = True

    if not is_new:
        await _send_ack(websocket, msg.message_id, "duplicate", trace_id)
        inbound_messages_total.labels(tenant_id=tenant_id, status="duplicate").inc()
        logger.info("重复消息", message_id=msg.message_id)
        return

    try:
        await _publish_inbound(exchange, tenant_id=tenant_id, user_id=user_id, msg=msg, trace_id=trace_id)
    except Exception:
        # 投递失败：把刚写的 dedup key 删掉，否则客户端重试会被误判成重复而收不到任何处理；
        # 这次 delete 本身失败（Redis 又恰好在这个瞬间挂了）不影响主流程，最多是那个 key 按
        # DEDUP_TTL_SECONDS 自然过期，不会一直占着
        try:
            await redis_client.delete(dedup_key)
        except RedisError:
            pass
        await _send_error(websocket, msg.message_id, "mq_publish_failed", "消息投递失败，请重试")
        inbound_messages_total.labels(tenant_id=tenant_id, status="error").inc()
        logger.error("投递到 MQ 失败", message_id=msg.message_id, exc_info=True)
        return

    await _send_ack(websocket, msg.message_id, "accepted", trace_id)
    inbound_messages_total.labels(tenant_id=tenant_id, status="accepted").inc()
    ack_latency_seconds.observe(time.monotonic() - start)
