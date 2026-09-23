"""消费 inbound.messages，失败分两类：可预期的降级（ack）和不可预期的异常（reject 进死信）。"""
import json
import time

import aio_pika
from aio_pika.abc import AbstractRobustConnection

from app.common.config import get_settings
from app.common.logging import bind_trace_context, clear_trace_context, get_logger
from app.common.mq import declare_topology, get_confirm_channel, get_connection

from app.worker.handler import process_inbound_message
from app.worker.metrics import messages_total, process_seconds

settings = get_settings()
logger = get_logger(__name__)


async def _on_message(message: aio_pika.IncomingMessage) -> None:
    headers = message.headers or {}
    trace_id = headers.get("trace_id")
    tenant_id = headers.get("tenant_id")
    bind_trace_context(trace_id=trace_id, tenant_id=tenant_id)
    start = time.monotonic()

    try:
        payload = json.loads(message.body)
        conversation_id_raw = payload["conversation_id"]
        user_id = payload["user_id"]
        client_message_id = payload["message_id"]
        content = payload["content"]
    except (json.JSONDecodeError, KeyError, TypeError):
        logger.error("消息体格式不对，进死信", exc_info=True)
        await message.reject(requeue=False)
        messages_total.labels(result="dead_letter").inc()
        process_seconds.observe(time.monotonic() - start)
        clear_trace_context()
        return

    try:
        result = await process_inbound_message(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id_raw=conversation_id_raw,
            client_message_id=client_message_id,
            content=content,
            trace_id=trace_id,
        )
        await message.ack()
        messages_total.labels(result=result).inc()
    except Exception:
        # process_inbound_message 已经把"可预期"的情况都处理掉了，走到这里说明是真正的 bug 或数据损坏
        logger.error("处理消息出现不可预期异常，进死信", exc_info=True)
        await message.reject(requeue=False)
        messages_total.labels(result="dead_letter").inc()
    finally:
        process_seconds.observe(time.monotonic() - start)
        clear_trace_context()


async def run_consumer() -> AbstractRobustConnection:
    connection = await get_connection()
    channel = await get_confirm_channel(connection)
    await channel.set_qos(prefetch_count=settings.mq_prefetch_count)
    _, inbound_queue, _ = await declare_topology(channel)
    await inbound_queue.consume(_on_message)
    logger.info("worker 开始消费 inbound.messages", prefetch_count=settings.mq_prefetch_count)
    return connection
