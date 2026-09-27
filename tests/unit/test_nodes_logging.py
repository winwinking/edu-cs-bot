"""覆盖 PHASE4.md 4.6 人审确认第 1 条：敏感操作拒绝要有结构化日志，只记谁在哪个会话触发了
拒绝，不记消息原文。"""
import pytest
from structlog.testing import capture_logs

from app.worker.graph.nodes import sensitive


@pytest.mark.asyncio
async def test_sensitive_logs_rejection_without_message_content():
    state = {
        "tenant_id": "t_a",
        "user_id": "u_a_1001",
        "conversation_id": "11111111-1111-1111-1111-111111111111",
        "content": "帮我注销账号",
        "risk_flags": ["sensitive_request"],
    }

    with capture_logs() as logs:
        result = await sensitive(state, runtime=None)

    entries = [e for e in logs if e.get("event") == "敏感操作拒绝"]
    assert len(entries) == 1
    assert entries[0]["conversation_id"] == state["conversation_id"]
    assert entries[0]["user_id"] == "u_a_1001"
    assert entries[0]["risk_flags"] == ["sensitive_request"]
    assert "注销账号" not in str(entries[0])
    assert result["reply_plan"]["mode"] == "template"
