"""覆盖 PHASE3.md 第 3 步验证清单：低于阈值不生成、超过阈值生成、摘要只能放在 user 消息里
（不能放进 system prompt）、摘要存库前已脱敏。

跟 tests/unit/test_handoff.py 是同一套思路：能用纯函数/monkeypatch 覆盖的就不连真实数据库/
LLM；真正连数据库、连 mock-llm 的完整链路（25 条消息触发摘要生成）走 phase3_smoke.py。
"""
from types import SimpleNamespace

import httpx
import pytest
from openai import APITimeoutError

from app.common.config import get_settings
from app.worker.graph import classify as classify_module
from app.worker.graph import context_summary as context_summary_module
from app.worker.graph.context_summary import (
    _generate_summary_text,
    append_summary_block,
    build_summary_messages,
    should_regenerate_summary,
)

settings = get_settings()


# ---------- 阈值判断 ----------


def test_below_threshold_does_not_regenerate():
    assert should_regenerate_summary(0) is False
    assert should_regenerate_summary(settings.conversation_history_limit) is False


def test_above_threshold_regenerates():
    assert should_regenerate_summary(settings.conversation_history_limit + 1) is True


# ---------- 摘要只能放在 user 消息里，不能放进 system prompt ----------


def test_append_summary_block_returns_unchanged_content_without_summary():
    assert append_summary_block("你好", None) == "你好"
    assert append_summary_block("你好", "") == "你好"


def test_append_summary_block_appends_history_summary_tag():
    result = append_summary_block("你好", "用户之前问过退费政策")
    assert result.startswith("你好")
    assert "<历史摘要>" in result
    assert "用户之前问过退费政策" in result
    assert result.endswith("</历史摘要>")


@pytest.mark.asyncio
async def test_classify_puts_summary_in_user_message_not_system(monkeypatch):
    captured = {}

    async def fake_chat_completion(**kwargs):
        captured.update(kwargs)
        message = SimpleNamespace(tool_calls=[])
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    monkeypatch.setattr(classify_module, "chat_completion", fake_chat_completion)

    state = {
        "tenant_id": "t_a",
        "user_id": "u_a_1001",
        "content": "你好",
        "history": [],
        "history_summary": "用户之前问过退费政策",
        "tenant_timezone": "Asia/Shanghai",
    }

    await classify_module._classify_with_llm(state)

    messages = captured["messages"]
    system_messages = [m["content"] for m in messages if m["role"] == "system"]
    user_messages = [m["content"] for m in messages if m["role"] == "user"]

    assert not any("用户之前问过退费政策" in c for c in system_messages)
    assert any("用户之前问过退费政策" in c for c in user_messages)
    assert any("<历史摘要>" in c for c in user_messages)


def test_build_summary_messages_keeps_transcript_out_of_system_message():
    messages = build_summary_messages("旧摘要文本", [{"role": "user", "content": "我的手机号是13812345678"}])
    system_messages = [m["content"] for m in messages if m["role"] == "system"]
    user_messages = [m["content"] for m in messages if m["role"] == "user"]

    assert not any("13812345678" in c for c in system_messages)
    assert any("13812345678" in c for c in user_messages)
    assert any("旧摘要文本" in c for c in user_messages)


# ---------- 摘要存库前必须脱敏 ----------


@pytest.mark.asyncio
async def test_generated_summary_is_masked_before_returning(monkeypatch):
    async def fake_chat_completion(**kwargs):
        message = SimpleNamespace(content="用户留了手机号13812345678，要求回电")
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    monkeypatch.setattr(context_summary_module, "chat_completion", fake_chat_completion)

    result = await _generate_summary_text(None, [{"role": "user", "content": "我的手机号是13812345678"}])

    assert "13812345678" not in result
    assert "138****5678" in result


@pytest.mark.asyncio
async def test_generate_summary_returns_none_on_llm_failure(monkeypatch):
    async def fake_chat_completion(**kwargs):
        raise APITimeoutError(httpx.Request("POST", "http://mock-llm:8000/v1/chat/completions"))

    monkeypatch.setattr(context_summary_module, "chat_completion", fake_chat_completion)

    result = await _generate_summary_text("旧摘要", [{"role": "user", "content": "你好"}])
    assert result is None


@pytest.mark.asyncio
async def test_generate_summary_returns_none_on_empty_llm_output(monkeypatch):
    async def fake_chat_completion(**kwargs):
        message = SimpleNamespace(content="   ")
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    monkeypatch.setattr(context_summary_module, "chat_completion", fake_chat_completion)

    result = await _generate_summary_text(None, [{"role": "user", "content": "你好"}])
    assert result is None
