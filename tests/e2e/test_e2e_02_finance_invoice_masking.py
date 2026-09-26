"""题目 6.3 场景 2：用户查询发票，机器人返回脱敏结果。

三件事：回复里邮箱打码、数据库里存的也是打码后的内容（不是先脱敏展示、库里却留了原文这种
"展示层脱敏、存储层没脱敏"的假脱敏）、审计日志记了这次成功查询。
"""
import uuid

import pytest
from sqlalchemy import select

from app.common.db import AsyncSessionLocal
from app.common.models import AuditLog, Message, MessageRole

from tests.e2e._helpers import conversation_id, poll_until, send_and_wait


@pytest.mark.asyncio
async def test_invoice_query_reply_and_stored_message_are_both_masked():
    conv_label = f"e2e_s2_{uuid.uuid4().hex[:8]}"
    conv_id = conversation_id("t_a", "u_a_1001", conv_label)

    result = await send_and_wait("t_a", "u_a_1001", conv_label, "我上个月的发票开了吗？")

    # 1) 回复内容：邮箱打码
    assert "l***@example.com" in result["reply"]
    assert "lin.xiaoyu@example.com" not in result["reply"]

    # 2) 数据库状态：落库的内容也是打码后的，不是原文。worker 先把回复发给客户端、发完才落库
    # （见 app/worker/handler.py），要轮询，不能假设收到回复时已经写完
    async def _fetch_message():
        async with AsyncSessionLocal() as session:
            return (
                await session.execute(
                    select(Message)
                    .where(
                        Message.tenant_id == "t_a",
                        Message.conversation_id == uuid.UUID(conv_id),
                        Message.role == MessageRole.assistant,
                    )
                    .order_by(Message.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()

    row = await poll_until(_fetch_message)
    assert row is not None, "assistant 回复应该落库"
    assert "lin.xiaoyu@example.com" not in row.content
    assert "l***@example.com" in row.content

    async with AsyncSessionLocal() as session:
        # 3) 审计：这次查询记了一条成功的 query_finance（写在 finance() 节点内部、respond()
        # 之前就已经提交，不受上面那个"先回复后落库"的时序影响，这里查一次就够）
        audit = (
            await session.execute(
                select(AuditLog)
                .where(AuditLog.tenant_id == "t_a", AuditLog.conversation_id == uuid.UUID(conv_id))
                .order_by(AuditLog.created_at.desc())
                .limit(1)
            )
        ).scalar_one()

    assert audit.action == "query_finance"
    assert audit.result == "success"
    assert audit.actor_user_id == "u_a_1001"
    assert audit.target_user_id == "u_a_1001"
