"""覆盖题目 6.1 点名的"幂等"在 worker 这一侧的核心逻辑（app/worker/handler.py）。

跟 gateway 那一层的 Redis SETNX 去重（tests/unit/test_gateway_rate_limit_dedup.py）不是同一道
防线：这里测的是消息真正落库之后的幂等——(tenant_id, message_id) 唯一约束 + status 字段，
用来区分"真重复"（已经回复过，跳过）和"上次处理到一半就崩了"（还要继续走完）。

不连真实数据库：手写一个假 session，只实现这几个函数用到的 execute/add/flush/rollback，
用一个队列按调用顺序喂结果——跟 test_gateway_rate_limit_dedup.py 手写假 redis_client/exchange
是同一个思路，保持"tests/unit 不启动任何服务"的分层原则。
"""
import uuid
from collections import namedtuple

import pytest
from sqlalchemy.exc import IntegrityError

from app.common.models import Conversation, Message, MessageRole, MessageStatus
from app.worker import handler as handler_module
from app.worker.handler import (
    ConversationForbidden,
    _insert_assistant_message,
    _load_recent_messages,
    _mark_user_message_replied,
    _metric_result,
    _resolve_conversation,
    _upsert_user_message,
    process_inbound_message,
)


class _FakeResult:
    def __init__(self, *, first=None, scalar=None):
        self._first = first
        self._scalar = scalar

    def first(self):
        return self._first

    def scalar_one(self):
        return self._scalar

    def scalar_one_or_none(self):
        return self._scalar


class _FakeSession:
    """按调用顺序消费预先准备好的 execute 结果；add/flush/rollback/commit 只记调用次数，
    flush 可以配置成抛异常，用来模拟"两个 worker 同时创建同一个新会话"的竞态。"""

    def __init__(self, execute_results, *, flush_raises: bool = False):
        self._execute_results = list(execute_results)
        self._flush_raises = flush_raises
        self.added: list = []
        self.flush_calls = 0
        self.rollback_calls = 0
        self.commit_calls = 0

    async def execute(self, stmt):
        return self._execute_results.pop(0)

    def add(self, obj) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flush_calls += 1
        if self._flush_raises and self.flush_calls == 1:
            raise IntegrityError("insert", {}, Exception("duplicate key"))

    async def rollback(self) -> None:
        self.rollback_calls += 1

    async def commit(self) -> None:
        self.commit_calls += 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


# ---------- _upsert_user_message：三种结果 ----------


@pytest.mark.asyncio
async def test_upsert_brand_new_message_is_inserted():
    session = _FakeSession([_FakeResult(first=(uuid.uuid4(),))])

    outcome = await _upsert_user_message(session, "t_a", uuid.uuid4(), "m1", "你好", "trace1")

    assert outcome == "inserted"


@pytest.mark.asyncio
async def test_upsert_conflict_but_not_yet_replied_is_retry():
    # ON CONFLICT DO NOTHING 没插进去（first() 为空），说明这条 message_id 之前来过；
    # 再查一次 status 发现还是 received，说明上次崩在半路，要重新走一遍生成回复
    session = _FakeSession(
        [_FakeResult(first=None), _FakeResult(scalar=MessageStatus.received)]
    )

    outcome = await _upsert_user_message(session, "t_a", uuid.uuid4(), "m1", "你好", "trace1")

    assert outcome == "retry"


@pytest.mark.asyncio
async def test_upsert_conflict_and_already_replied_is_done():
    session = _FakeSession(
        [_FakeResult(first=None), _FakeResult(scalar=MessageStatus.replied)]
    )

    outcome = await _upsert_user_message(session, "t_a", uuid.uuid4(), "m1", "你好", "trace1")

    assert outcome == "done"


# ---------- _resolve_conversation：新建 / 已存在且归属正确 / 越权 / 并发创建竞态 ----------


@pytest.mark.asyncio
async def test_resolve_conversation_creates_new_one_when_absent():
    conversation_id = uuid.uuid4()
    session = _FakeSession([_FakeResult(scalar=None)])

    result = await _resolve_conversation(session, "t_a", "u_a_1001", str(conversation_id))

    assert result == conversation_id
    assert session.added  # 新会话被 add 进 session
    assert session.flush_calls == 1


@pytest.mark.asyncio
async def test_resolve_conversation_returns_existing_when_owner_matches():
    conversation_id = uuid.uuid4()
    existing = Conversation(id=conversation_id, tenant_id="t_a", user_id="u_a_1001")
    session = _FakeSession([_FakeResult(scalar=existing)])

    result = await _resolve_conversation(session, "t_a", "u_a_1001", str(conversation_id))

    assert result == conversation_id
    assert not session.added  # 已存在，不会再创建一次


@pytest.mark.asyncio
async def test_resolve_conversation_raises_forbidden_when_owner_mismatches():
    conversation_id = uuid.uuid4()
    # 会话真实归属是 u_a_1001，但这次消息声称自己是 u_a_1002——越权
    existing = Conversation(id=conversation_id, tenant_id="t_a", user_id="u_a_1001")
    session = _FakeSession([_FakeResult(scalar=existing)])

    with pytest.raises(ConversationForbidden):
        await _resolve_conversation(session, "t_a", "u_a_1002", str(conversation_id))


@pytest.mark.asyncio
async def test_resolve_conversation_handles_concurrent_creation_race():
    # 两个 worker 几乎同时收到同一个新会话的第一条消息：本地判断"不存在"之后再插入时撞了唯一约束，
    # 应该回滚、重新查一次，把对方创建的那条当成最终结果，而不是让这次请求整体失败
    conversation_id = uuid.uuid4()
    existing = Conversation(id=conversation_id, tenant_id="t_a", user_id="u_a_1001")
    session = _FakeSession(
        [_FakeResult(scalar=None), _FakeResult(scalar=existing)], flush_raises=True
    )

    result = await _resolve_conversation(session, "t_a", "u_a_1001", str(conversation_id))

    assert result == conversation_id
    assert session.rollback_calls == 1


# ---------- _metric_result：纯函数 ----------


def test_metric_result_marks_rule_fallback_as_llm_degraded():
    assert _metric_result("rule_fallback") == "llm_degraded"


def test_metric_result_marks_rule_and_llm_and_none_as_ok():
    assert _metric_result("rule") == "ok"
    assert _metric_result("llm") == "ok"
    assert _metric_result(None) == "ok"


# ---------- 历史消息加载 / 写回复消息 / 标记已回复：纯 DB 读写，跟幂等逻辑配套 ----------

_Row = namedtuple("_Row", ["role", "content"])


class _AllResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


@pytest.mark.asyncio
async def test_load_recent_messages_reverses_to_oldest_first_and_excludes_current():
    # 数据库按 created_at 倒序取出（新的在前），喂给 LLM 前要反转成"旧的在前，新的在后"；
    # 当前这条消息不该出现在历史里（避免在上下文里出现两次）
    rows = [_Row(MessageRole.assistant, "最新回复"), _Row(MessageRole.user, "第一句")]
    session = _FakeSession([_AllResult(rows)])

    history = await _load_recent_messages(session, "t_a", uuid.uuid4(), 10, exclude_message_id="m_current")

    assert history == [
        {"role": "user", "content": "第一句"},
        {"role": "assistant", "content": "最新回复"},
    ]


@pytest.mark.asyncio
async def test_insert_assistant_message_adds_message_with_intent_and_meta():
    session = _FakeSession([])
    conversation_id = uuid.uuid4()

    await _insert_assistant_message(
        session, "t_a", conversation_id, "已为你处理", "trace1", "finance_query", {"tools": []}
    )

    assert len(session.added) == 1
    saved = session.added[0]
    assert saved.role == MessageRole.assistant
    assert saved.content == "已为你处理"
    assert saved.intent == "finance_query"
    assert saved.meta == {"tools": []}


@pytest.mark.asyncio
async def test_mark_user_message_replied_executes_update():
    session = _FakeSession([None])

    await _mark_user_message_replied(session, "t_a", "m1")

    assert session._execute_results == []  # 唯一一次预置的 execute 结果已经被消费


# ---------- process_inbound_message：不碰 LangGraph 的两条早返回分支 ----------


@pytest.mark.asyncio
async def test_process_inbound_message_returns_forbidden_without_touching_graph(monkeypatch):
    published: list = []

    async def fake_resolve_conversation(session, tenant_id, user_id, conversation_id_raw):
        raise ConversationForbidden()

    async def fake_publish_error(tenant_id, user_id, message_id, code, detail):
        published.append((tenant_id, user_id, message_id, code, detail))

    monkeypatch.setattr(handler_module, "AsyncSessionLocal", lambda: _FakeSession([]))
    monkeypatch.setattr(handler_module, "_resolve_conversation", fake_resolve_conversation)
    monkeypatch.setattr(handler_module, "publish_error", fake_publish_error)

    result, intent = await process_inbound_message(
        tenant_id="t_a",
        user_id="u_a_1002",
        conversation_id_raw=str(uuid.uuid4()),
        client_message_id="m1",
        content="查一下别人的账单",
        trace_id="trace1",
    )

    assert (result, intent) == ("forbidden", None)
    assert published[0][3] == "forbidden"


@pytest.mark.asyncio
async def test_process_inbound_message_returns_duplicate_without_touching_graph(monkeypatch):
    async def fake_resolve_conversation(session, tenant_id, user_id, conversation_id_raw):
        return uuid.uuid4()

    async def fake_upsert(session, tenant_id, conversation_id, client_message_id, content, trace_id):
        return "done"

    monkeypatch.setattr(handler_module, "AsyncSessionLocal", lambda: _FakeSession([]))
    monkeypatch.setattr(handler_module, "_resolve_conversation", fake_resolve_conversation)
    monkeypatch.setattr(handler_module, "_upsert_user_message", fake_upsert)

    result, intent = await process_inbound_message(
        tenant_id="t_a",
        user_id="u_a_1001",
        conversation_id_raw=str(uuid.uuid4()),
        client_message_id="m1",
        content="重复的消息",
        trace_id="trace1",
    )

    assert (result, intent) == ("duplicate", None)
