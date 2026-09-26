"""题目 6.3 场景 5：用户创建"明天 9 点提醒"，到点后 5 秒内推送。

从 WebSocket 真实创建提醒（走 manage_reminder 工具调用的完整链路，不是像
tests/integration/test_reminder_scheduler_push.py 那样直接插库），再用跟
scripts/reminder_ff.py 相同的手法把 next_trigger_at 快进到"现在"，验证 scheduler 在 5 秒内
推送、并且推送内容真的进了消息表。

三件事：创建时的确认回复；到点推送（订阅 Redis 频道拿到）；数据库里多了一条
intent="reminder_push" 的 assistant 消息（scheduler 推送成功后自己写的，见
app/scheduler/loop.py）。
"""
import asyncio
import json
import uuid

import pytest
from sqlalchemy import func, select, update

from app.common.db import AsyncSessionLocal
from app.common.models import Message, Reminder, ReminderStatus
from app.common.redis import redis_client

from tests.e2e._helpers import conversation_id, poll_until, send_and_wait


@pytest.mark.asyncio
async def test_reminder_created_via_chat_is_pushed_within_5_seconds():
    conv_label = f"e2e_s5_{uuid.uuid4().hex[:8]}"
    conv_id = conversation_id("t_a", "u_a_1001", conv_label)
    marker_title = f"集成测试交作业{uuid.uuid4().hex[:6]}"

    # 1) 创建：走真实的 manage_reminder 工具调用
    result = await send_and_wait("t_a", "u_a_1001", conv_label, f"明天早上 9 点提醒我{marker_title}")
    assert "已设置提醒" in result["reply"]

    async with AsyncSessionLocal() as session:
        reminder = (
            await session.execute(
                select(Reminder).where(Reminder.tenant_id == "t_a", Reminder.title == marker_title)
            )
        ).scalar_one()
        reminder_id = reminder.id

        # 快进到"现在"，不用真的等到明天——跟 scripts/reminder_ff.py 同样的手法
        await session.execute(
            update(Reminder).where(Reminder.id == reminder_id).values(next_trigger_at=func.now())
        )
        await session.commit()

    # 2) 到点推送：订阅 Redis 频道，5 秒内应该收到
    channel = "im:out:t_a:u_a_1001"
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(channel)
    try:
        pushed = None
        deadline = asyncio.get_event_loop().time() + 5
        while asyncio.get_event_loop().time() < deadline:
            raw = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.5)
            if raw is None:
                continue
            payload = json.loads(raw["data"])
            if payload.get("reminder_id") == str(reminder_id):
                pushed = payload
                break
        assert pushed is not None, "scheduler 应该在 5 秒内推送这条到期提醒"
        assert marker_title in pushed["text"]
    finally:
        await pubsub.unsubscribe(channel)
        await pubsub.aclose()

    # 3) 数据库状态：scheduler 推送成功后自己写了一条 intent=reminder_push 的消息。scheduler
    # 是先发 Redis 推送、批量处理完当前所有到期提醒后才一次性 commit（见 app/scheduler/loop.py
    # "先推送再提交"），拿到 Redis 推送和这行 INSERT 提交完成不是同一时刻，要轮询；另外要按
    # meta.reminder_id 精确匹配这一条，不能假设"最近一条 reminder_push 消息"就是自己这条——
    # 其它并发场景/演示留下的提醒也可能在差不多的时间点触发
    async def _fetch_pushed_message():
        async with AsyncSessionLocal() as session:
            return (
                await session.execute(
                    select(Message).where(
                        Message.tenant_id == "t_a",
                        Message.intent == "reminder_push",
                        Message.meta["reminder_id"].astext == str(reminder_id),
                    )
                )
            ).scalar_one_or_none()

    pushed_message = await poll_until(_fetch_pushed_message)
    assert pushed_message is not None, "scheduler 推送成功后应该写一条 reminder_push 消息"

    async with AsyncSessionLocal() as session:
        refreshed_reminder = (
            await session.execute(select(Reminder).where(Reminder.id == reminder_id))
        ).scalar_one()

    assert marker_title in pushed_message.content
    # repeat=none（默认）：推送完标成 done，不会再触发第二次
    assert refreshed_reminder.status == ReminderStatus.done
