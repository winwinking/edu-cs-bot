"""覆盖题目 6.2 点名的两项：IM 入站（gateway 收到消息、回 ACK、RabbitMQ 里多一条）、
队列（worker 消费后 ACK；格式坏的消息进死信队列）。

用真实 gateway/worker/RabbitMQ（`make up` 起的那一套），不 mock 任何一层——这正是集成测试
要测的"部件接在一起会不会出问题"，跟 tests/unit 里手写假 exchange/session 的单测是两回事。

队列深度会被 worker 立刻消费掉，直接比"发消息前后 inbound.messages 的当前长度"会因为 worker
消费速度而不稳定（flaky）；改用 RabbitMQ management API 里的 message_stats 累计计数
（publish/ack 只增不减），比对发消息前后的差值，不受 worker 消费快慢影响。
"""
import asyncio
import uuid

import aio_pika
import pytest

from app.common.mq import INBOUND_ROUTING_KEY, declare_topology, get_confirm_channel, get_connection

from tests.integration._helpers import (
    ack_count,
    conversation_id,
    publish_count,
    rabbitmq_queue_stats,
    send_and_wait,
)


@pytest.mark.asyncio
async def test_gateway_ack_and_publish_to_rabbitmq():
    before = await rabbitmq_queue_stats("inbound.messages")

    result = await send_and_wait("t_a", "u_a_1001", "int_inbound", "你好，在吗")

    assert result["ack"] == "accepted"

    # RabbitMQ management 插件的 message_stats 是按固定间隔（默认约 5 秒）汇总的快照，不是
    # publish 那一刻就实时更新，所以要轮询等它反映出来，不能发完消息立刻查一次就断言
    deadline = asyncio.get_event_loop().time() + 10
    published = False
    while asyncio.get_event_loop().time() < deadline:
        after = await rabbitmq_queue_stats("inbound.messages")
        if publish_count(after) >= publish_count(before) + 1:
            published = True
            break
        await asyncio.sleep(0.5)

    assert published, "gateway 应该把这条消息发布到 inbound.messages 队列"


@pytest.mark.asyncio
async def test_worker_consumes_and_acks_the_message():
    before = await rabbitmq_queue_stats("inbound.messages")

    result = await send_and_wait("t_a", "u_a_1001", "int_worker_ack", "寒假班请假会退课时费吗")
    assert result["ack"] == "accepted"
    assert result["reply"]  # worker 真的跑完了图、生成了回复

    # worker 消费+处理这条消息需要时间（意图识别 + 检索 + 生成），reply_end 已经收到说明
    # process_inbound_message 跑完了，但 ack 这个 RabbitMQ 层面的动作是 consumer 拿到回复之后
    # 才做的最后一步，稳妥起见轮询确认，而不是假设 reply_end 一收到 ack 计数必然已经加过
    deadline = asyncio.get_event_loop().time() + 10
    acked = False
    while asyncio.get_event_loop().time() < deadline:
        after = await rabbitmq_queue_stats("inbound.messages")
        if ack_count(after) >= ack_count(before) + 1:
            acked = True
            break
        await asyncio.sleep(0.5)

    assert acked, "worker 应该在处理完消息后 ack 这条 RabbitMQ 消息"


@pytest.mark.asyncio
async def test_malformed_message_goes_to_dead_letter_queue():
    # 直接绕过 gateway，往 im.inbound 交换机发一条格式坏的消息体（缺 content 字段）——
    # 跟 worker/consumer.py 的 _on_message() 判断逻辑对上：json.loads 能过，但取 payload["content"]
    # 会 KeyError，属于"消息体本身坏了"，不走重试，直接 reject(requeue=False) 进死信
    connection = await get_connection()
    try:
        channel = await get_confirm_channel(connection)
        inbound_exchange, _, dead_queue = await declare_topology(channel)

        marker = f"integration-test-{uuid.uuid4()}"
        bad_body = f'{{"tenant_id": "t_a", "user_id": "u_a_1001", "message_id": "{marker}", "conversation_id": "{uuid.uuid4()}"}}'.encode()
        message = aio_pika.Message(
            body=bad_body,
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            content_type="application/json",
            headers={"trace_id": "test-trace", "tenant_id": "t_a"},
        )
        await inbound_exchange.publish(message, routing_key=INBOUND_ROUTING_KEY, timeout=5)

        # 轮询死信队列，把不属于这次测试的消息原样放回去（用 requeue=True 通过 reject 放回队列
        # 头部），只消费掉我们自己发的那条，不破坏其它并行测试/演示留下的死信数据
        deadline = asyncio.get_event_loop().time() + 10
        found = False
        seen_others: list[aio_pika.IncomingMessage] = []
        while asyncio.get_event_loop().time() < deadline and not found:
            msg = await dead_queue.get(no_ack=False, fail=False)
            if msg is None:
                await asyncio.sleep(0.5)
                continue
            if marker.encode() in msg.body:
                await msg.ack()
                found = True
            else:
                seen_others.append(msg)
        for msg in seen_others:
            await msg.nack(requeue=True)

        assert found, "格式坏的消息应该出现在死信队列 inbound.dead 里"
    finally:
        await connection.close()
