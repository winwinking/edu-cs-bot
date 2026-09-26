"""故障注入 7「格式坏的消息进死信」专用小工具（PHASE4.md 4.5）：gateway 自己的入站校验不会
放过格式错误的消息，没法从 WebSocket 这一层造出这个故障，得绕过 gateway 直接往
inbound.messages 发一条 body 不是合法 JSON 的消息，才能验证 app/worker/consumer.py 里
"消息体解析失败，不重试、直接进死信"这条分支（PHASE3.md 设计决定 12）。

用法：
  docker compose run --rm tools python scripts/publish_malformed_message.py
"""
import asyncio
import uuid

import aio_pika

from app.common.mq import INBOUND_ROUTING_KEY, declare_topology, get_confirm_channel, get_connection


async def main() -> None:
    connection = await get_connection()
    try:
        channel = await get_confirm_channel(connection)
        inbound_exchange, _, _ = await declare_topology(channel)
        message = aio_pika.Message(
            body=b"{this is not valid json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            headers={"trace_id": str(uuid.uuid4()), "tenant_id": "t_a", "x-retry-count": 0},
        )
        await inbound_exchange.publish(message, routing_key=INBOUND_ROUTING_KEY)
        print("已发送一条格式坏的消息，几秒内应该能在 inbound.dead 看到它，worker 日志会有一条 alert")
    finally:
        await connection.close()


if __name__ == "__main__":
    asyncio.run(main())
