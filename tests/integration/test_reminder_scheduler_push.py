"""覆盖题目 6.2 点名的"提醒调度：插一条马上到期的提醒，scheduler 在 5 秒内推送并算出下一次时间"。

直接往数据库插一条 Reminder（不经过 worker 的 manage_reminder 工具调用），因为这里要测的是
scheduler 这一个组件本身——它是不是真的每秒扫描到期提醒、真的推到 Redis、真的按重复规则算出
下一次触发时间，跟"用户怎么创建提醒"是两件事（那部分归 tests/unit/test_reminder_tool_args.py
和 tests/e2e 场景 5 管）。scheduler 是 `make up` 起的真实容器，一直在跑，不需要在测试里手动启动。
"""
import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.common.db import AsyncSessionLocal
from app.common.models import Conversation, Reminder, ReminderRepeat, ReminderStatus
from app.common.redis import redis_client


@pytest.mark.asyncio
async def test_due_reminder_is_pushed_within_5_seconds_and_next_trigger_is_recomputed():
    tenant_id, user_id = "t_a", "u_a_1001"
    conversation_id = uuid.uuid4()
    reminder_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    original_event_at = now - timedelta(seconds=1)  # 已经"到期"

    async with AsyncSessionLocal() as session:
        session.add(Conversation(id=conversation_id, tenant_id=tenant_id, user_id=user_id))
        session.add(
            Reminder(
                id=reminder_id,
                tenant_id=tenant_id,
                user_id=user_id,
                conversation_id=conversation_id,
                title="集成测试提醒",
                event_at=original_event_at,
                timezone="Asia/Shanghai",
                repeat=ReminderRepeat.daily,
                advance_minutes=0,
                next_trigger_at=original_event_at,
                status=ReminderStatus.active,
            )
        )
        await session.commit()

    channel = f"im:out:{tenant_id}:{user_id}"
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(channel)
    try:
        pushed_payload = None
        deadline = asyncio.get_event_loop().time() + 5
        while asyncio.get_event_loop().time() < deadline:
            raw = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.5)
            if raw is None:
                continue
            payload = json.loads(raw["data"])
            if payload.get("reminder_id") == str(reminder_id):
                pushed_payload = payload
                break

        assert pushed_payload is not None, "scheduler 应该在 5 秒内把这条到期提醒推到 Redis"
        assert pushed_payload["title"] == "集成测试提醒"
        assert "集成测试提醒" in pushed_payload["text"]
    finally:
        await pubsub.unsubscribe(channel)
        await pubsub.aclose()

    # scheduler 是"先推送、批量处理完这一轮所有到期提醒后才一次性 commit"（先推送再提交，见
    # app/scheduler/loop.py），收到 Redis 推送的那一刻，这一行 next_trigger_at 的更新不一定
    # 已经提交，要轮询，不能查一次就断言
    updated = None
    deadline = asyncio.get_event_loop().time() + 5
    while asyncio.get_event_loop().time() < deadline:
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(Reminder).where(Reminder.id == reminder_id))
            updated = result.scalar_one()
        if updated.next_trigger_at > original_event_at:
            break
        await asyncio.sleep(0.2)

    # repeat=daily：推送完不会标 done，而是算出下一次触发时间（大约 24 小时后），继续 active
    assert updated.status == ReminderStatus.active
    assert updated.next_trigger_at > original_event_at + timedelta(hours=23)
    assert updated.event_at > original_event_at
