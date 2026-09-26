"""题目 6.3 场景 1：用户查询课程政策，机器人引用知识库回答。

三件事都要看：回复内容（带出处）、数据库状态（assistant 消息落库、citations 记进 meta）、
meta（intent/tools 状态）。跟 scripts/phase2_smoke.py 的场景 1 是同一句测试用语，但这里额外
多查了一次数据库——冒烟脚本只看 WebSocket 回包，不验证"最终真的写进数据库了"。
"""
import uuid

import pytest
from sqlalchemy import select

from app.common.db import AsyncSessionLocal
from app.common.models import Message, MessageRole

from tests.e2e._helpers import conversation_id, poll_until, send_and_wait


@pytest.mark.asyncio
async def test_knowledge_policy_question_cites_knowledge_base():
    conv_label = f"e2e_s1_{uuid.uuid4().hex[:8]}"
    conv_id = conversation_id("t_a", "u_a_1001", conv_label)

    result = await send_and_wait("t_a", "u_a_1001", conv_label, "寒假班请假会退课时费吗？")

    # 1) 回复内容：带出处
    assert result["reply"].startswith("依据《课程服务协议》第 4.2 条")
    assert len(result["meta"].get("citations", [])) > 0

    # 2) meta：意图和工具状态
    assert result["meta"]["intent"] == "knowledge_qa"
    tool_status = next(t["status"] for t in result["meta"]["tools"] if t["name"] == "search_knowledge")
    assert tool_status == "ok"

    # 3) 数据库状态：assistant 消息真的落库，intent/citations 跟回包一致。worker 是先把回复
    # 流式发给客户端、发完才落库（见 app/worker/handler.py），reply_end 到达客户端和这行 INSERT
    # 提交之间隔着一次数据库往返，查询要轮询，不能假设收到 reply_end 时已经写完
    async def _fetch():
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

    row = await poll_until(_fetch)
    assert row is not None, "assistant 回复应该落库"
    assert row.content == result["reply"]
    assert row.intent == "knowledge_qa"
    assert row.meta["citations"] == result["meta"]["citations"]
