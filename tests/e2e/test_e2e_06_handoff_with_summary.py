"""题目 6.3 场景 6：用户说"转人工"，携带摘要转接。

三件事：回复确认已转接（在线/不在线两种话术都算数）；meta 带 handoff_ticket_id；数据库里的
HandoffTicket 记录了摘要、意图、已尝试操作、风险标记——这是"转接时携带会话摘要、用户意图、
已尝试操作、风险提示"（FR-7）落到数据库里的证据，不能只看回复文案。
"""
import uuid

import pytest
from sqlalchemy import select

from app.common.db import AsyncSessionLocal
from app.common.models import HandoffTicket

from tests.e2e._helpers import conversation_id, send_and_wait


@pytest.mark.asyncio
async def test_handoff_ticket_carries_summary_intent_and_attempted_actions():
    conv_label = f"e2e_s6_{uuid.uuid4().hex[:8]}"
    conv_id = conversation_id("t_a", "u_a_1001", conv_label)

    # 先问一句财务问题，制造"已尝试操作"的历史，再要求转人工
    await send_and_wait("t_a", "u_a_1001", conv_label, "我上个月的发票开了吗？")
    result = await send_and_wait("t_a", "u_a_1001", conv_label, "转人工")

    # 1) 回复内容：在线/不在线两种话术都算成功转接
    assert ("已为你转接人工客服" in result["reply"]) or ("人工客服现在不在线" in result["reply"])
    ticket_id = result["meta"]["handoff_ticket_id"]
    assert ticket_id is not None

    # 2) meta：意图是 handoff
    assert result["meta"]["intent"] == "handoff"

    # 3) 数据库状态：摘要、意图、已尝试操作、风险标记都落库了
    async with AsyncSessionLocal() as session:
        ticket = (
            await session.execute(select(HandoffTicket).where(HandoffTicket.id == uuid.UUID(ticket_id)))
        ).scalar_one()

    assert ticket.tenant_id == "t_a"
    assert ticket.user_id == "u_a_1001"
    assert ticket.conversation_id == uuid.UUID(conv_id)
    assert ticket.summary  # 摘要非空
    assert len(ticket.attempted_actions) > 0  # 至少记了刚才那次财务查询
    assert isinstance(ticket.risk_flags, list)
