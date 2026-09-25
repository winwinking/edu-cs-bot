"""消费 inbound.messages，失败分两类：可预期的降级（ack）和不可预期的异常。

不可预期异常按设计决定 12 分情况处理：消息体本身坏了（解析不出来），重试也没用，直接进
inbound.dead；处理中出现意外异常（比如数据库连不上），读消息头 x-retry-count（没有就是 0），
小于 DLQ_MAX_RETRIES 就把消息重新投回原队列、次数加 1，等于就进 inbound.dead——重试之间没有
等待间隔（已知问题，写进 AGENT_LOG）。
"""
import json
import time
from functools import partial

import aio_pika
from aio_pika.abc import AbstractExchange, AbstractRobustConnection

from app.common.config import get_settings
from app.common.logging import bind_trace_context, clear_trace_context, get_logger
from app.common.mq import INBOUND_ROUTING_KEY, declare_topology, get_confirm_channel, get_connection

from app.worker.handler import process_inbound_message
from app.worker.metrics import messages_total, process_seconds

settings = get_settings()
logger = get_logger(__name__)


async def _requeue_with_retry(
    inbound_exchange: AbstractExchange, message: aio_pika.IncomingMessage, retry_count: int
) -> None:
    """重新投回原队列，次数加 1。用发布 + ack 原消息实现"重新入队"，不用 nack(requeue=True)——
    后者会把消息原样放回队列头部，没法顺手把 x-retry-count 改掉；发布走的是开了 publisher
    confirm 的 channel，等 broker 真的收到新消息才会往下 ack 原消息，不会两边都丢或者两边都有。
    """
    new_headers = dict(message.headers or {})
    new_headers["x-retry-count"] = retry_count
    new_message = aio_pika.Message(
        body=message.body,
        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        content_type=message.content_type,
        headers=new_headers,
    )
    await inbound_exchange.publish(
        new_message, routing_key=INBOUND_ROUTING_KEY, timeout=settings.mq_connect_timeout_seconds
    )
    await message.ack()


async def _on_message(message: aio_pika.IncomingMessage, *, inbound_exchange: AbstractExchange) -> None:
    headers = message.headers or {}
    trace_id = headers.get("trace_id")
    tenant_id = headers.get("tenant_id")
    retry_count = int(headers.get("x-retry-count", 0) or 0)
    bind_trace_context(trace_id=trace_id, tenant_id=tenant_id)
    start = time.monotonic()

    try:
        payload = json.loads(message.body)
        conversation_id_raw = payload["conversation_id"]
        user_id = payload["user_id"]
        client_message_id = payload["message_id"]
        content = payload["content"]
    except (json.JSONDecodeError, KeyError, TypeError):
        # 消息本身坏了，重试也不会变好，不走重试直接进死信
        logger.error("消息体格式不对，进死信", exc_info=True)
        await message.reject(requeue=False)
        messages_total.labels(result="dead_letter", intent="").inc()
        process_seconds.observe(time.monotonic() - start)
        clear_trace_context()
        return

    try:
        result, intent = await process_inbound_message(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id_raw=conversation_id_raw,
            client_message_id=client_message_id,
            content=content,
            trace_id=trace_id,
        )
        await message.ack()
        messages_total.labels(result=result, intent=intent or "").inc()
    except Exception:
        # process_inbound_message 已经把"可预期"的情况都处理掉了，走到这里说明是真正的 bug 或
        # 意外故障（比如数据库连不上）
        if retry_count < settings.dlq_max_retries:
            logger.warning(
                "处理消息出现不可预期异常，重新投回原队列", retry_count=retry_count + 1, exc_info=True
            )
            await _requeue_with_retry(inbound_exchange, message, retry_count + 1)
            messages_total.labels(result="retry", intent="").inc()
        else:
            logger.error("处理消息出现不可预期异常，重试次数用完，进死信", retry_count=retry_count, exc_info=True)
            await message.reject(requeue=False)
            messages_total.labels(result="dead_letter", intent="").inc()
    finally:
        process_seconds.observe(time.monotonic() - start)
        clear_trace_context()


async def run_consumer() -> AbstractRobustConnection:
    connection = await get_connection()
    channel = await get_confirm_channel(connection)
    await channel.set_qos(prefetch_count=settings.mq_prefetch_count)
    inbound_exchange, inbound_queue, _ = await declare_topology(channel)
    await inbound_queue.consume(partial(_on_message, inbound_exchange=inbound_exchange))
    logger.info("worker 开始消费 inbound.messages", prefetch_count=settings.mq_prefetch_count)
    return connection
