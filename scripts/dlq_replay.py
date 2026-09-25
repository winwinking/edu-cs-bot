"""死信重投脚本（PHASE3.md 第 5 步）：把 inbound.dead 里积压的消息重新投回 inbound.messages，
重试次数清零。用于故障恢复后手工补处理（比如数据库挂了一段时间，期间失败的消息都进了死信，
数据库恢复后用这个脚本把它们捞回来重新处理一遍），也是阶段五排障演练要用的工具。

用法：
  docker compose run --rm tools python scripts/dlq_replay.py
"""
import asyncio

import aio_pika

from app.common.config import get_settings
from app.common.mq import INBOUND_ROUTING_KEY, declare_topology, get_confirm_channel, get_connection

settings = get_settings()


async def main() -> None:
    connection = await get_connection()
    try:
        channel = await get_confirm_channel(connection)
        inbound_exchange, _, dead_queue = await declare_topology(channel)

        replayed = 0
        while True:
            # basic.get 一条一条取，取到 None 说明死信队列已经空了；不用 consume，这是一次性
            # 批处理脚本，不是常驻服务
            message = await dead_queue.get(no_ack=False, fail=False)
            if message is None:
                break

            new_headers = dict(message.headers or {})
            new_headers["x-retry-count"] = 0
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
            replayed += 1

        print(f"重投了 {replayed} 条消息")
    finally:
        await connection.close()


if __name__ == "__main__":
    asyncio.run(main())
