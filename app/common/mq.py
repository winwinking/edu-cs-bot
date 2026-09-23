"""aio-pika 连接与拓扑声明。

拓扑：
  exchange im.inbound (direct, durable) -> queue inbound.messages (durable)
  inbound.messages 配置了 x-dead-letter-exchange=im.dlx，处理失败 reject 后自动进死信
  exchange im.dlx (direct, durable) -> queue inbound.dead (durable)
"""
from typing import Tuple

import aio_pika
from aio_pika import ExchangeType
from aio_pika.abc import AbstractChannel, AbstractQueue, AbstractRobustConnection

from app.common.config import get_settings

settings = get_settings()

INBOUND_EXCHANGE = "im.inbound"
INBOUND_QUEUE = "inbound.messages"
INBOUND_ROUTING_KEY = "inbound.messages"
DLX_EXCHANGE = "im.dlx"
DEAD_QUEUE = "inbound.dead"
DEAD_ROUTING_KEY = "inbound.dead"


async def get_connection() -> AbstractRobustConnection:
    # connect_robust 断线会自动重连，但首次建连还是要有超时，不能无限等
    return await aio_pika.connect_robust(
        settings.rabbitmq_url,
        timeout=settings.mq_connect_timeout_seconds,
    )


async def get_confirm_channel(connection: AbstractRobustConnection) -> AbstractChannel:
    # publisher confirms：等 broker 确认收到消息后才能回 ack，保证不丢消息
    channel = await connection.channel(publisher_confirms=True)
    return channel


async def declare_topology(channel: AbstractChannel) -> Tuple[AbstractQueue, AbstractQueue]:
    """声明 inbound 主队列和死信队列，返回 (inbound_queue, dead_queue)"""
    dlx_exchange = await channel.declare_exchange(DLX_EXCHANGE, ExchangeType.DIRECT, durable=True)
    dead_queue = await channel.declare_queue(DEAD_QUEUE, durable=True)
    await dead_queue.bind(dlx_exchange, routing_key=DEAD_ROUTING_KEY)

    inbound_exchange = await channel.declare_exchange(INBOUND_EXCHANGE, ExchangeType.DIRECT, durable=True)
    inbound_queue = await channel.declare_queue(
        INBOUND_QUEUE,
        durable=True,
        arguments={
            "x-dead-letter-exchange": DLX_EXCHANGE,
            "x-dead-letter-routing-key": DEAD_ROUTING_KEY,
        },
    )
    await inbound_queue.bind(inbound_exchange, routing_key=INBOUND_ROUTING_KEY)
    return inbound_queue, dead_queue
