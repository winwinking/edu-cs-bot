"""清空 inbound.dead 死信队列（PHASE4.md 4.5，配合故障注入 7 的恢复步骤用）。

跟 scripts/dlq_replay.py 不是一回事：dlq_replay 是把死信"重新处理一遍"，故障注入 7 造出来的
消息 body 本身就不是合法 JSON，重投回去只会立刻又进一次死信，起不到"恢复演示环境"的作用；
这里只是单纯把队列清空，回到"死信队列是空的"这个初始状态。用 RabbitMQ 自己的 purge，不用
管理接口的 HTTP API，是因为那个接口要额外传 RabbitMQ 密码，这里走应用已经配好的 RABBITMQ_URL
就够了，命令里不用出现任何密码。

用法：
  docker compose run --rm tools python scripts/purge_dead_queue.py
"""
import asyncio

from app.common.mq import declare_topology, get_confirm_channel, get_connection


async def main() -> None:
    connection = await get_connection()
    try:
        channel = await get_confirm_channel(connection)
        _, _, dead_queue = await declare_topology(channel)
        purged = await dead_queue.purge()
        print(f"已清空 inbound.dead：{purged}")
    finally:
        await connection.close()


if __name__ == "__main__":
    asyncio.run(main())
