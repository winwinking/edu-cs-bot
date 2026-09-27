"""覆盖 PHASE4.md 4.6 人审发现：故障注入时靠 trace_id 在 worker 日志里搜不到知识问答路径的
任何一行结构化日志，没法确认这次检索用的是什么 query、命中了哪些条款、有没有低于阈值、
OutputGuard 有没有生效。这里分别测 knowledge() 的检索日志和 respond() 的 OutputGuard 日志。
"""
import pytest
from structlog.testing import capture_logs

from app.common.retrieval import SearchResult
from app.worker.graph import graph as graph_module
from app.worker.graph import knowledge as knowledge_module


class _FakeRetriever:
    min_score = 0.30

    async def search(self, tenant_id: str, query: str, top_k: int = 3):
        return [
            SearchResult(
                doc_id="d1", doc_title="课程服务协议", clause_no="4.2", clause_title="", content="原文A", score=0.5
            ),
            SearchResult(
                doc_id="d2", doc_title="退费政策", clause_no="2.1", clause_title="", content="原文B", score=0.1
            ),
        ]


class _FakeRuntimeContext:
    session = None


class _FakeRuntime:
    context = _FakeRuntimeContext()


async def _fake_get_daily_budget(session, tenant_id):
    return None


async def _fake_is_budget_exceeded(tenant_id, tenant_timezone, budget):
    return False


@pytest.mark.asyncio
async def test_knowledge_logs_query_scores_and_threshold_flag(monkeypatch):
    monkeypatch.setattr(knowledge_module, "get_retriever", lambda: _FakeRetriever())
    monkeypatch.setattr(knowledge_module, "get_daily_budget", _fake_get_daily_budget)
    monkeypatch.setattr(knowledge_module, "is_budget_exceeded", _fake_is_budget_exceeded)

    state = {
        "tenant_id": "t_a",
        "conversation_id": "conv-1",
        "content": "寒假班放假安排是什么",
        "history": [],
    }

    with capture_logs() as logs:
        await knowledge_module.knowledge(state, _FakeRuntime())

    entries = [entry for entry in logs if entry.get("event") == "知识检索完成"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["conversation_id"] == "conv-1"
    assert entry["query"] == "寒假班放假安排是什么"
    assert entry["min_score"] == 0.30
    assert entry["tool_status"] == "ok"
    # 只记条款编号、分数、是否低于阈值，不带条款原文
    assert entry["results"] == [
        {"doc_title": "课程服务协议", "clause_no": "4.2", "score": 0.5, "below_threshold": False},
        {"doc_title": "退费政策", "clause_no": "2.1", "score": 0.1, "below_threshold": True},
    ]
    for result in entry["results"]:
        assert "content" not in result
        assert "原文" not in str(result)


@pytest.mark.asyncio
async def test_knowledge_retrieval_failure_still_logs_query(monkeypatch):
    class _TimeoutRetriever:
        min_score = 0.30

        async def search(self, tenant_id, query, top_k=3):
            import httpx

            raise httpx.TimeoutException("超时")

    monkeypatch.setattr(knowledge_module, "get_retriever", lambda: _TimeoutRetriever())
    monkeypatch.setattr(knowledge_module, "get_daily_budget", _fake_get_daily_budget)
    monkeypatch.setattr(knowledge_module, "is_budget_exceeded", _fake_is_budget_exceeded)

    state = {
        "tenant_id": "t_a",
        "conversation_id": "conv-2",
        "content": "寒假班放假安排是什么",
        "history": [],
    }

    with capture_logs() as logs:
        result = await knowledge_module.knowledge(state, _FakeRuntime())

    assert result["tools_meta"] == [{"name": "search_knowledge", "status": "timeout"}]
    warn_entries = [entry for entry in logs if entry.get("event") == "知识检索超时，按无命中处理"]
    assert warn_entries[0]["query"] == "寒假班放假安排是什么"
    assert warn_entries[0]["conversation_id"] == "conv-2"
    ok_entries = [entry for entry in logs if entry.get("event") == "知识检索完成"]
    assert ok_entries[0]["results"] == []


def _generate_state(fallback_text: str = "我查到的相关规定是：原文A") -> dict:
    return {
        "tenant_id": "t_a",
        "conversation_id": "conv-3",
        "reply_plan": {
            "mode": "generate",
            "messages": [],
            "allowed_citations": [["课程服务协议", "4.2"]],
            "lead_in": "依据《课程服务协议》第 4.2 条：",
            "fallback_text": fallback_text,
        },
    }


async def _noop(*args, **kwargs):
    return None


def _patch_respond_dependencies(monkeypatch, stream_fn):
    monkeypatch.setattr(graph_module, "stream_chat_completion", stream_fn)
    monkeypatch.setattr(graph_module, "publish_reply_chunk", _noop)
    monkeypatch.setattr(graph_module, "publish_reply_end", _noop)
    monkeypatch.setattr(graph_module, "_record_generate_usage", _noop)


@pytest.mark.asyncio
async def test_respond_logs_dropped_citations_and_fallback_used_when_all_dropped(monkeypatch):
    async def fake_stream(messages, usage_holder=None):
        # 唯一一句话引用了没被检索到的出处，会被 OutputGuard 整句丢掉，触发"输出第一条条款
        # 原文"兜底
        yield "根据《课程服务协议》第 9.9 条，所有课程都可以随时全额退款。"

    _patch_respond_dependencies(monkeypatch, fake_stream)
    state = _generate_state()

    with capture_logs() as logs:
        await graph_module.respond("t_a", "u_a_1001", "msg-1", state)

    entries = [entry for entry in logs if entry.get("event") == "知识问答 OutputGuard 核对完成"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["conversation_id"] == "conv-3"
    assert entry["dropped_sentences"] == 1
    assert entry["dropped_citations"] == [["课程服务协议", "9.9"]]
    assert entry["fallback_used"] is True


@pytest.mark.asyncio
async def test_respond_logs_no_drop_and_no_fallback_when_citation_allowed(monkeypatch):
    async def fake_stream(messages, usage_holder=None):
        yield "寒假班从 1 月 20 日开始放假，2 月 20 日结束。"

    _patch_respond_dependencies(monkeypatch, fake_stream)
    state = _generate_state()

    with capture_logs() as logs:
        await graph_module.respond("t_a", "u_a_1001", "msg-1", state)

    entries = [entry for entry in logs if entry.get("event") == "知识问答 OutputGuard 核对完成"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["dropped_sentences"] == 0
    assert entry["dropped_citations"] == []
    assert entry["fallback_used"] is False


@pytest.mark.asyncio
async def test_respond_does_not_log_guard_summary_for_non_knowledge_reply(monkeypatch):
    async def fake_stream(messages, usage_holder=None):
        yield "你好，我在。"

    _patch_respond_dependencies(monkeypatch, fake_stream)
    state = {
        "tenant_id": "t_a",
        "conversation_id": "conv-4",
        # chitchat 的真实 reply_plan 形状（app/worker/graph/nodes.py 的 chitchat()）：
        # allowed_citations 是空列表而不是缺失/None，日志判断必须用真值而不是 is not None，
        # 否则会把闲聊也误判成知识问答记一条同名日志
        "reply_plan": {"mode": "generate", "messages": [], "citations": [], "allowed_citations": []},
    }

    with capture_logs() as logs:
        await graph_module.respond("t_a", "u_a_1001", "msg-1", state)

    entries = [entry for entry in logs if entry.get("event") == "知识问答 OutputGuard 核对完成"]
    assert entries == []
