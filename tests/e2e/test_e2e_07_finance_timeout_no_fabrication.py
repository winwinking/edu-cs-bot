"""题目 6.3 场景 7：财务系统超时，机器人不编造，回复"暂时查不到，已记录，稍后回复"。

用 mockctl 同款方式（POST /admin/config）把 mock-finance 切到 timeout 模式，测完无论成功
失败都要切回 normal——这是本文件里唯一一个"故障注入"场景，按 PHASE4.md 要求测完必须恢复。

三件事：回复不含任何金额/订单号数字（没有编造）；数据库里写了一条 FollowupTask（"已记录，
稍后回复"不是一句空话）；审计日志记了 upstream_error。
"""
import re
import uuid

import pytest
from sqlalchemy import select

from app.common.db import AsyncSessionLocal
from app.common.models import AuditLog, FollowupTask
from app.worker.graph.style import FINANCE_UPSTREAM_ERROR_REPLY

from tests.e2e._helpers import conversation_id, reset_mock, send_and_wait, set_mock_mode


@pytest.mark.asyncio
async def test_finance_timeout_does_not_fabricate_and_records_followup():
    conv_label = f"e2e_s7_{uuid.uuid4().hex[:8]}"
    conv_id = conversation_id("t_a", "u_a_1001", conv_label)

    await set_mock_mode("finance", mode="timeout")
    try:
        result = await send_and_wait("t_a", "u_a_1001", conv_label, "我上个月的发票开了吗？")
    finally:
        await reset_mock("finance")

    # 1) 回复内容：固定话术，不含任何金额/订单号——没有编造数据
    assert result["reply"] == FINANCE_UPSTREAM_ERROR_REPLY
    assert "¥" not in result["reply"]
    assert not re.search(r"EDU-\d+", result["reply"])
    assert not re.search(r"\d", result["reply"])  # 连数字都不该出现

    # 2) & 3) 数据库状态：跟进任务 + 审计记录
    async with AsyncSessionLocal() as session:
        followup = (
            await session.execute(
                select(FollowupTask)
                .where(FollowupTask.tenant_id == "t_a", FollowupTask.conversation_id == uuid.UUID(conv_id))
                .order_by(FollowupTask.created_at.desc())
                .limit(1)
            )
        ).scalar_one()
        audit = (
            await session.execute(
                select(AuditLog)
                .where(AuditLog.tenant_id == "t_a", AuditLog.conversation_id == uuid.UUID(conv_id))
                .order_by(AuditLog.created_at.desc())
                .limit(1)
            )
        ).scalar_one()

    assert followup.kind == "finance_query"
    assert followup.query["finance_kind"] == "invoices"
    assert audit.result == "upstream_error"
