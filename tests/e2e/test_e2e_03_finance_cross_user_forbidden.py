"""题目 6.3 场景 3：用户 A 查询用户 B 财务，返回 403；另外直接调 mock-finance 也应该拒绝。

三件事：回复是拒绝话术（worker 层）、审计日志里有一条 forbidden、直接绕过 worker 用 A 的身份
调 mock-finance 查 B 也被拒绝（两层权限校验里独立的第二层，见 app/worker/graph/finance.py
顶部注释）——这一条对应题目原文"另外直接用 A 的 token 调 mock-finance 查 B，HTTP 403"。
"""
import uuid

import pytest
from sqlalchemy import select

from app.common.db import AsyncSessionLocal
from app.common.finance_client import FinanceForbidden, fetch_finance_data
from app.common.models import AuditLog
from app.worker.graph.style import FINANCE_FORBIDDEN_REPLY

from tests.e2e._helpers import conversation_id, send_and_wait


@pytest.mark.asyncio
async def test_user_a_querying_user_b_finance_is_forbidden_end_to_end():
    conv_label = f"e2e_s3_{uuid.uuid4().hex[:8]}"
    conv_id = conversation_id("t_a", "u_a_1001", conv_label)

    # 1) 回复内容：worker 这一层的拒绝话术
    result = await send_and_wait("t_a", "u_a_1001", conv_label, "帮我查一下 u_a_1004 的发票")
    assert result["reply"] == FINANCE_FORBIDDEN_REPLY
    tool_status = next(t["status"] for t in result["meta"]["tools"] if t["name"] == "query_finance")
    assert tool_status == "forbidden"

    # 2) 数据库状态：审计日志记了一条 forbidden
    async with AsyncSessionLocal() as session:
        audit = (
            await session.execute(
                select(AuditLog)
                .where(AuditLog.tenant_id == "t_a", AuditLog.conversation_id == uuid.UUID(conv_id))
                .order_by(AuditLog.created_at.desc())
                .limit(1)
            )
        ).scalar_one()

    assert audit.action == "query_finance"
    assert audit.result == "forbidden"
    assert audit.actor_user_id == "u_a_1001"
    assert audit.target_user_id == "u_a_1004"

    # 3) 绕开 worker，直接以 u_a_1001 的身份查 u_a_1004——mock-finance 自己也要拒绝
    with pytest.raises(FinanceForbidden):
        await fetch_finance_data(
            "invoices", tenant_id="t_a", acting_user_id="u_a_1001", target_user_id="u_a_1004"
        )
