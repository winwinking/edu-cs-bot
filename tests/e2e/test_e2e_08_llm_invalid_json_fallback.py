"""题目 6.3 场景 8：LLM 返回非法 JSON，系统兜底，不执行工具。

用 mockctl 同款方式把 mock-llm 切到 invalid_json 模式，测完恢复。用"帮我把自动续费关了"
这句话——如果系统真的没有正确兜底、把非法参数硬当成合法工具调用执行了，这本该是一次高风险
指令，一旦被误执行后果比闲聊问题更容易暴露出来。

三件事：回复是兜底话术；meta 显示工具状态是 invalid_json；数据库里没有生成任何 PendingAction
（证明真的没有执行任何工具，不只是回复文案对了但背地里悄悄操作了）。
"""
import uuid

import pytest
from sqlalchemy import select

from app.common.db import AsyncSessionLocal
from app.common.models import PendingAction
from app.worker.graph.style import FALLBACK_INVALID_OUTPUT_REPLY

from tests.e2e._helpers import conversation_id, reset_mock, send_and_wait, set_mock_mode


@pytest.mark.asyncio
async def test_llm_invalid_json_falls_back_without_executing_any_tool():
    conv_label = f"e2e_s8_{uuid.uuid4().hex[:8]}"
    conv_id = conversation_id("t_a", "u_a_1001", conv_label)

    await set_mock_mode("llm", mode="invalid_json")
    try:
        result = await send_and_wait("t_a", "u_a_1001", conv_label, "帮我把自动续费关了")
    finally:
        await set_mock_mode("llm", mode="normal")

    # 1) 回复内容：兜底话术（LLM 输出非法，不是"LLM 调不通"，用词是 FALLBACK_INVALID_OUTPUT_REPLY）
    assert result["reply"] == FALLBACK_INVALID_OUTPUT_REPLY

    # 2) meta：工具状态是 invalid_json，不是 ok/pending_confirmation
    tool_status = next(t["status"] for t in result["meta"]["tools"] if t["name"] == "platform_command")
    assert tool_status == "invalid_json"

    # 3) 数据库状态：没有生成任何待确认操作——真的没有执行/发起任何工具调用
    async with AsyncSessionLocal() as session:
        pending_rows = (
            await session.execute(
                select(PendingAction).where(
                    PendingAction.tenant_id == "t_a", PendingAction.conversation_id == uuid.UUID(conv_id)
                )
            )
        ).scalars().all()

    assert pending_rows == []
