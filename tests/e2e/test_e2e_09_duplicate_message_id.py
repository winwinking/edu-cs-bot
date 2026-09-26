"""题目 6.3 场景 9：同一个 message_id 发两次，第二次回 duplicate，数据库里只有一条。

三件事：第一次 ack=accepted 且真的收到回复；第二次 ack=duplicate 且不会再收到一次 reply_end；
数据库里这个 message_id 对应的 user 消息只有一条，不是两条。
"""
import uuid

import pytest
from sqlalchemy import select

from app.common.db import AsyncSessionLocal
from app.common.models import Message, MessageRole

from tests.e2e._helpers import send_and_wait


@pytest.mark.asyncio
async def test_duplicate_message_id_is_only_processed_once():
    conv_label = f"e2e_s9_{uuid.uuid4().hex[:8]}"
    mid = str(uuid.uuid4())

    # 1) 第一次：正常处理
    r1 = await send_and_wait("t_a", "u_a_1001", conv_label, "你好，在吗", message_id=mid)
    assert r1["ack"] == "accepted"
    assert r1["reply"]

    # 2) 第二次：重复，不再触发新的回复
    r2 = await send_and_wait("t_a", "u_a_1001", conv_label, "你好，在吗", message_id=mid)
    assert r2["ack"] == "duplicate"
    assert r2["reply"] == ""

    # 3) 数据库状态：这个 message_id 的 user 消息只有一条
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(Message).where(
                    Message.tenant_id == "t_a", Message.message_id == mid, Message.role == MessageRole.user
                )
            )
        ).scalars().all()

    assert len(rows) == 1
