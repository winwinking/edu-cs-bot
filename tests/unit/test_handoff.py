"""覆盖 PHASE2.md 2.11 的几类逻辑：
1. 摘要生成失败时的模板兜底怎么拼（纯函数，不连网络）。
2. 摘要必须经过 mask_text() 脱敏——用假的 chat_completion 强制走兜底路径，验证兜底文本里的
   手机号被脱敏（这条路径不需要真的连 mock-llm，兜底文本本身就来自对话原文）。
3. 坐席状态查询失败（PlatformUnavailable）时，handoff() 按不在线处理，转接记录状态是
   left_message，回复话术是"不在线"那句——用手写的假 session（不连真实数据库）验证。
4. handoff_tickets.intent 的查询要把 handoff 和 dissatisfied_first 都排除在外（人审发现
   dissatisfied_first 原来没排除，见 AGENT_LOG 步骤 2.11）——不连真实数据库，直接检查
   _last_business_intent() 传给 session.execute() 的 SQL 语句本身有没有把这两个值都排除掉。

真正连数据库、连 mock-platform 的完整链路走 2.11 验证脚本里的真实端到端对话，这里只测
上面几类不需要真实数据库/网络就能验证的行为。
"""
import uuid
from types import SimpleNamespace

import httpx
import pytest
from openai import APITimeoutError
from sqlalchemy.dialects import postgresql
from structlog.testing import capture_logs

from app.worker.graph import handoff as handoff_module
from app.worker.graph.handoff import (
    _build_offline_reply,
    _build_online_reply,
    _fallback_summary,
    _generate_summary,
    _last_business_intent,
    handoff,
)
from app.common.models import HandoffStatus
from app.common.platform_client import PlatformUnavailable


def test_fallback_summary_picks_last_three_user_messages():
    messages = [
        {"role": "user", "content": "第一条"},
        {"role": "assistant", "content": "回复一"},
        {"role": "user", "content": "第二条"},
        {"role": "assistant", "content": "回复二"},
        {"role": "user", "content": "第三条"},
        {"role": "user", "content": "第四条"},
    ]
    assert _fallback_summary(messages) == "第二条；第三条；第四条"


def test_fallback_summary_truncates_each_message_to_fifty_chars():
    long_text = "很长的一句话" * 20  # 超过 50 字
    messages = [{"role": "user", "content": long_text}]
    result = _fallback_summary(messages)
    assert result == long_text[:50]
    assert len(result) == 50


def test_fallback_summary_ignores_assistant_messages():
    messages = [
        {"role": "assistant", "content": "系统回复，不该出现在摘要里"},
        {"role": "user", "content": "用户唯一的一条消息"},
    ]
    assert _fallback_summary(messages) == "用户唯一的一条消息"


def test_fallback_summary_empty_history_gives_empty_string():
    assert _fallback_summary([]) == ""


def test_build_online_reply_includes_queue_length_and_wait_minutes():
    reply = _build_online_reply(3, 5)
    assert "前面还有 3 位" in reply
    assert "预计 5 分钟接入" in reply


def test_build_offline_reply_includes_service_hours():
    reply = _build_offline_reply("8:30 至 20:30")
    assert "服务时间是每天 8:30 至 20:30" in reply


@pytest.fixture(autouse=True)
def _no_budget_limit(monkeypatch):
    """阶段三第 6 步给 `_generate_summary` 加了 token 预算检查和用量记录，这两步都要连数据库/
    Redis，这里统一 monkeypatch 成"预算没超、记录是空操作"，不影响本文件其它纯函数测试。"""

    async def fake_get_daily_budget(session, tenant_id):
        return None

    async def fake_add_tokens_used(*args, **kwargs):
        return None

    async def fake_record_llm_usage(*args, **kwargs):
        return None

    monkeypatch.setattr(handoff_module, "get_daily_budget", fake_get_daily_budget)
    monkeypatch.setattr(handoff_module, "add_tokens_used", fake_add_tokens_used)
    monkeypatch.setattr(handoff_module, "record_llm_usage", fake_record_llm_usage)


@pytest.mark.asyncio
async def test_generate_summary_masks_phone_number_when_falling_back_to_template(monkeypatch):
    """chat_completion 调用失败 -> 走 _fallback_summary 模板兜底 -> mask_text() 脱敏，
    验证的是"兜底路径产出的文本最终也会被脱敏"这条链路，不是 mask_text() 本身（那个在
    tests/unit/test_masking.py 里已经测过）。"""

    async def fake_chat_completion(**kwargs):
        raise APITimeoutError(httpx.Request("POST", "http://mock-llm:8000/v1/chat/completions"))

    monkeypatch.setattr(handoff_module, "chat_completion", fake_chat_completion)

    state = {
        "tenant_id": "t_a",
        "tenant_timezone": "Asia/Shanghai",
        "content": "我的手机号是13812345678，麻烦联系我",
        "history": [],
    }

    summary = await _generate_summary(state, session=None)

    assert "13812345678" not in summary
    assert "138****5678" in summary


@pytest.mark.asyncio
async def test_generate_summary_falls_back_when_llm_returns_empty_content(monkeypatch):
    async def fake_chat_completion(**kwargs):
        message = SimpleNamespace(content="")
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    monkeypatch.setattr(handoff_module, "chat_completion", fake_chat_completion)

    state = {"tenant_id": "t_a", "tenant_timezone": "Asia/Shanghai", "content": "发票多久能开", "history": []}
    summary = await _generate_summary(state, session=None)

    assert summary == "发票多久能开"


class _FakeScalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeExecResult:
    def __init__(self, rows=None, first_row=None):
        self._rows = rows or []
        self._first_row = first_row

    def scalars(self):
        return _FakeScalars(self._rows)

    def first(self):
        return self._first_row


class _FakeSession:
    """handoff() 会依次查最近非转人工意图、审计记录、待确认操作两遍（一次列清单、一次风险标记）
    ——这几条查询在没有历史数据的会话里统统查不到东西，所以每次 execute() 都返回空结果，
    只需要正确支持 add()/commit() 记录下最终写了哪张 HandoffTicket。"""

    def __init__(self):
        self.added: list = []
        self.committed = 0

    async def execute(self, stmt):
        return _FakeExecResult()

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed += 1


def _handoff_state() -> dict:
    return {
        "tenant_id": "t_a",
        "user_id": "u_a_1001",
        "conversation_id": "11111111-1111-1111-1111-111111111111",
        "content": "转人工",
        "history": [],
        "handoff_trigger": "keyword",
        "risk_flags": [],
    }


@pytest.mark.asyncio
async def test_handoff_reports_left_message_when_agents_status_unavailable(monkeypatch):
    async def fake_chat_completion(**kwargs):
        message = SimpleNamespace(content="摘要")
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    async def fake_get_agents_status():
        raise PlatformUnavailable("mock-platform 超时")

    monkeypatch.setattr(handoff_module, "chat_completion", fake_chat_completion)
    monkeypatch.setattr(handoff_module, "get_agents_status", fake_get_agents_status)

    session = _FakeSession()
    runtime = SimpleNamespace(context=SimpleNamespace(session=session))

    with capture_logs() as logs:
        result = await handoff(_handoff_state(), runtime)

    assert "人工客服现在不在线" in result["reply_plan"]["text"]
    assert len(session.added) == 1
    assert session.added[0].status == HandoffStatus.left_message

    # PHASE4.md 4.6 人审确认第 1 条：转人工工单创建要有结构化日志，只记状态/ID，不记摘要原文
    entries = [e for e in logs if e.get("event") == "转人工工单创建"]
    assert len(entries) == 1
    assert entries[0]["conversation_id"] == _handoff_state()["conversation_id"]
    assert entries[0]["user_id"] == "u_a_1001"
    assert entries[0]["ticket_id"] == result["handoff_ticket_id"]
    assert entries[0]["trigger"] == "keyword"
    assert entries[0]["status"] == "left_message"
    assert entries[0]["online"] is False
    assert "摘要" not in str(entries[0])


class _CapturingSession:
    """只用来接住 _last_business_intent() 真正构造出来的 SQL 语句，不需要连数据库——
    直接把语句编译成带字面量的 SQL 文本，检查 WHERE 条件里排除的意图集合对不对。"""

    def __init__(self):
        self.stmt = None

    async def execute(self, stmt):
        self.stmt = stmt
        return _FakeExecResult()


@pytest.mark.asyncio
async def test_last_business_intent_excludes_both_handoff_and_dissatisfied_first():
    session = _CapturingSession()
    conversation_id = str(uuid.uuid4())

    await _last_business_intent(session, "t_a", conversation_id)

    compiled = str(session.stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "'handoff'" in compiled
    assert "'dissatisfied_first'" in compiled
