"""覆盖 2.10 人审时提出的两个边界（不连真实数据库/真实 LLM，session 和 chat_completion 都换成
本文件手写的假对象，只还原 classify() 关心的最小接口）：

1. 会话里有未过期的待确认操作时，用户问一句跟确认无关的正常问题（比如"发票多久能开"），
   应该按正常意图处理，不能被"模糊确认"的提醒话术拦下来。
2. 会话里很久以前有一条已经执行完的操作（超过它自己的有效期），用户现在说的话恰好含"确认"
   （比如"确认一下我的课表"，这里"确认"是普通动词），不应该被误路由到 confirm_action
   回复"这个操作已经处理过了"。
"""
import uuid
from types import SimpleNamespace

import pytest

from app.worker.graph import classify as classify_module


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _FakeSession:
    """execute() 对任何 select 语句都返回同一个"有没有查到行"的结果——每个测试场景里
    classify 实际只会走到一种待确认查询，不需要按语句内容区分返回值。"""

    def __init__(self, has_row: bool):
        self._row = ("fake-pending-action-id",) if has_row else None

    async def execute(self, stmt):
        return _FakeResult(self._row)


def _fake_chitchat_response() -> SimpleNamespace:
    message = SimpleNamespace(tool_calls=[])
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _fake_tool_call_response(name: str, arguments_json: str) -> SimpleNamespace:
    function = SimpleNamespace(name=name, arguments=arguments_json)
    message = SimpleNamespace(tool_calls=[SimpleNamespace(function=function)])
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _state(content: str) -> dict:
    return {
        "tenant_id": "t_a",
        "user_id": "u_a_1001",
        "conversation_id": str(uuid.uuid4()),
        "content": content,
        "history": [],
    }


@pytest.mark.asyncio
async def test_unrelated_question_during_active_pending_is_not_hijacked(monkeypatch):
    async def fake_chat_completion(**kwargs):
        return _fake_tool_call_response("search_knowledge", '{"query": "发票多久能开"}')

    monkeypatch.setattr(classify_module, "chat_completion", fake_chat_completion)

    session = _FakeSession(has_row=True)  # 有未过期的待确认
    state = _state("发票多久能开")

    result = await classify_module._classify_core(state, session, state["content"])

    assert result["intent"] == "knowledge_qa"


@pytest.mark.asyncio
async def test_bare_affirmation_during_active_pending_gets_ambiguous_nudge(monkeypatch):
    async def fake_chat_completion(**kwargs):
        return _fake_chitchat_response()

    monkeypatch.setattr(classify_module, "chat_completion", fake_chat_completion)

    session = _FakeSession(has_row=True)  # 有未过期的待确认
    state = _state("对")

    result = await classify_module._classify_core(state, session, state["content"])

    assert result["intent"] == "confirm_ambiguous"


@pytest.mark.asyncio
async def test_no_pending_action_at_all_bare_reply_is_plain_chitchat(monkeypatch):
    async def fake_chat_completion(**kwargs):
        return _fake_chitchat_response()

    monkeypatch.setattr(classify_module, "chat_completion", fake_chat_completion)

    session = _FakeSession(has_row=False)  # 压根没有待确认
    state = _state("对")

    result = await classify_module._classify_core(state, session, state["content"])

    assert result["intent"] == "chitchat"


@pytest.mark.asyncio
async def test_confirm_word_without_any_recent_pending_action_falls_through_to_llm(monkeypatch):
    # 会话里没有"有效期内"的待确认记录（比如很久以前那条已经过了 expires_at）——
    # "确认一下我的课表"里的"确认"不该被当成在回应旧操作，交给正常分类处理
    async def fake_chat_completion(**kwargs):
        return _fake_tool_call_response("platform_command", '{"action": "open_schedule"}')

    monkeypatch.setattr(classify_module, "chat_completion", fake_chat_completion)

    session = _FakeSession(has_row=False)  # _has_recent_pending_action 查不到任何行
    state = _state("确认一下我的课表")

    result = await classify_module._classify_core(state, session, state["content"])

    assert result["intent"] == "platform_command"
