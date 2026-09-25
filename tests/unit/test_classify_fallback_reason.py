"""覆盖第 6 步人审发现的问题：预算耗尽和熔断打开都会让意图识别降级为关键词规则，关键词规则
又判断不出意图时，两者要给用户不同的话术——熔断打开是"过一会儿再试可能就好了"，预算耗尽要等
第二天才恢复，不能说"稍后再试"，见 AGENT_LOG 步骤 6 的人审记录。

`_classify_with_llm` 本身只负责把 fallback_reason 定成哪个值，具体话术由 app/worker/graph/
nodes.py 的 `fallback()` 节点选，这里两段分别测。
"""
import pytest

from app.common.circuit_breaker import CircuitBreakerOpenError
from app.worker.graph import classify as classify_module
from app.worker.graph import nodes as nodes_module
from app.worker.graph.style import (
    FALLBACK_BUDGET_EXCEEDED_REPLY,
    FALLBACK_INVALID_OUTPUT_REPLY,
    FALLBACK_LLM_UNAVAILABLE_REPLY,
)


def _state() -> dict:
    return {
        "tenant_id": "t_a",
        "user_id": "u_a_1001",
        "content": "你好",  # 不命中任何关键词规则，落到 fallback 意图
        "history": [],
        "tenant_timezone": "Asia/Shanghai",
    }


@pytest.mark.asyncio
async def test_budget_exceeded_and_no_keyword_match_uses_budget_reason(monkeypatch):
    async def fake_is_budget_exceeded(tenant_id, tenant_timezone, budget):
        return True

    monkeypatch.setattr(classify_module, "get_daily_budget", lambda session, tenant_id: _async_none())
    monkeypatch.setattr(classify_module, "is_budget_exceeded", fake_is_budget_exceeded)

    result = await classify_module._classify_with_llm(_state(), session=None)

    assert result["intent"] == "fallback"
    assert result["fallback_reason"] == "budget_exceeded"
    assert result["budget_exceeded"] is True
    assert "circuit_breaker" not in result


@pytest.mark.asyncio
async def test_circuit_open_and_no_keyword_match_keeps_llm_unavailable_reason(monkeypatch):
    async def fake_is_budget_exceeded(tenant_id, tenant_timezone, budget):
        return False

    async def fake_chat_completion(**kwargs):
        raise CircuitBreakerOpenError("llm")

    monkeypatch.setattr(classify_module, "get_daily_budget", lambda session, tenant_id: _async_none())
    monkeypatch.setattr(classify_module, "is_budget_exceeded", fake_is_budget_exceeded)
    monkeypatch.setattr(classify_module, "chat_completion", fake_chat_completion)

    result = await classify_module._classify_with_llm(_state(), session=None)

    assert result["intent"] == "fallback"
    assert result["fallback_reason"] == "llm_unavailable"
    assert result["circuit_breaker"] == ["llm"]
    assert "budget_exceeded" not in result


async def _async_none():
    return None


@pytest.mark.asyncio
async def test_fallback_node_picks_budget_exceeded_reply():
    state = {"fallback_reason": "budget_exceeded"}
    result = await nodes_module.fallback(state, runtime=None)
    assert result["reply_plan"]["text"] == FALLBACK_BUDGET_EXCEEDED_REPLY


@pytest.mark.asyncio
async def test_fallback_node_picks_llm_unavailable_reply():
    state = {"fallback_reason": "llm_unavailable"}
    result = await nodes_module.fallback(state, runtime=None)
    assert result["reply_plan"]["text"] == FALLBACK_LLM_UNAVAILABLE_REPLY


@pytest.mark.asyncio
async def test_fallback_node_defaults_to_invalid_output_reply():
    state = {"fallback_reason": "invalid_output"}
    result = await nodes_module.fallback(state, runtime=None)
    assert result["reply_plan"]["text"] == FALLBACK_INVALID_OUTPUT_REPLY
