"""编图（PHASE2.md 2.7 第 2、4 点）：load_context -> classify -> 条件路由 -> 业务节点 -> END。

respond() 是图外面的统一出口：图只负责决策，产出 reply_plan，不直接往外发消息；respond() 拿到
跑完图的最终 state，负责把 reply_plan 转成真正的 reply_chunk/reply_end 发给客户端，template 按句
切分，generate 流式调 LLM 并经过 OutputGuard，最后统一发 reply_end（带 meta）。
"""
import time
from typing import Any

from langgraph.graph import END, StateGraph
from openai import APIConnectionError, APIError, APITimeoutError

from app.common.llm_client import stream_chat_completion
from app.common.logging import get_logger
from app.worker.graph.classify import classify
from app.worker.graph.guard import OutputGuard
from app.worker.graph.nodes import chitchat, fallback, load_context, placeholder, reminder_stub, sensitive
from app.worker.graph.state import GraphContext, GraphState
from app.worker.graph.style import FALLBACK_LLM_UNAVAILABLE_REPLY
from app.worker.metrics import first_token_seconds
from app.worker.pubsub import publish_reply_chunk, publish_reply_end

logger = get_logger(__name__)

# 意图 -> 节点名。knowledge/finance/command/handoff/confirm_action/cancel_action/request_confirmation
# 本步都还是占位节点（见 nodes.py 的 placeholder），阶段二后续步骤逐个换成真正的业务节点
_INTENT_TO_NODE = {
    "knowledge_qa": "knowledge",
    "finance_query": "finance",
    "platform_command": "command",
    "reminder": "reminder_stub",
    "chitchat": "chitchat",
    "handoff": "handoff",
    "confirm_action": "confirm_action",
    "cancel_action": "cancel_action",
    "fallback": "fallback",
}

_BUSINESS_NODES = (
    "knowledge",
    "finance",
    "command",
    "request_confirmation",
    "confirm_action",
    "cancel_action",
    "reminder_stub",
    "handoff",
    "chitchat",
    "sensitive",
    "fallback",
)


def _route(state: GraphState) -> str:
    intent = state.get("intent")
    if intent == "high_risk":
        # 敏感操作关键词命中的 high_risk 走 sensitive 的固定拒绝话术；LLM 选出高风险平台指令的
        # high_risk 走 request_confirmation（本步占位，2.10 会实现真正的二次确认）
        if "sensitive_request" in state.get("risk_flags", []):
            return "sensitive"
        return "request_confirmation"
    return _INTENT_TO_NODE.get(intent, "fallback")


def _build_graph():
    graph = StateGraph(state_schema=GraphState, context_schema=GraphContext)

    graph.add_node("load_context", load_context)
    graph.add_node("classify", classify)
    graph.add_node("knowledge", placeholder)
    graph.add_node("finance", placeholder)
    graph.add_node("command", placeholder)
    graph.add_node("request_confirmation", placeholder)
    graph.add_node("confirm_action", placeholder)
    graph.add_node("cancel_action", placeholder)
    graph.add_node("reminder_stub", reminder_stub)
    graph.add_node("handoff", placeholder)
    graph.add_node("chitchat", chitchat)
    graph.add_node("sensitive", sensitive)
    graph.add_node("fallback", fallback)

    graph.set_entry_point("load_context")
    graph.add_edge("load_context", "classify")
    graph.add_conditional_edges("classify", _route, {name: name for name in _BUSINESS_NODES})
    for name in _BUSINESS_NODES:
        graph.add_edge(name, END)

    return graph.compile()


COMPILED_GRAPH = _build_graph()


def _build_meta(state: GraphState, guard: OutputGuard) -> dict[str, Any]:
    return {
        "intent": state.get("intent"),
        "route_source": state.get("route_source"),
        "tools": state.get("tools_meta", []),
        "citations": state.get("citations", []),
        "pending_action_id": state.get("pending_action_id"),
        "handoff_ticket_id": state.get("handoff_ticket_id"),
        "guard": {
            "dropped_sentences": guard.dropped_sentences,
            "banned_phrases_removed": guard.banned_phrases_removed,
        },
        "risk_flags": state.get("risk_flags", []),
    }


async def respond(tenant_id: str, user_id: str, reply_to: str, state: GraphState) -> tuple[str, dict[str, Any]]:
    plan = state["reply_plan"]
    guard = OutputGuard()
    seq = 0
    chunks: list[str] = []

    async def emit(sentences: list[str]) -> None:
        nonlocal seq
        for sentence in sentences:
            await publish_reply_chunk(tenant_id, user_id, reply_to, seq, sentence)
            chunks.append(sentence)
            seq += 1

    if plan["mode"] == "template":
        await emit(guard.feed(plan["text"]))
        await emit(guard.flush())
    else:
        first_token_seen = False
        start = time.monotonic()
        try:
            async for delta in stream_chat_completion(plan["messages"]):
                if not first_token_seen:
                    first_token_seconds.observe(time.monotonic() - start)
                    first_token_seen = True
                await emit(guard.feed(delta))
            await emit(guard.flush())
        except (APIError, APITimeoutError, APIConnectionError) as exc:
            logger.warning("生成回复时 LLM 调用失败", error=str(exc))
            if not chunks:
                # 一个分片都还没发出去，整体换成"LLM 不可用"固定话术；已经发出去的内容不回滚，
                # 保留目前的 guard 统计（不重新计一遍）
                fallback_guard = OutputGuard()
                await emit(fallback_guard.feed(FALLBACK_LLM_UNAVAILABLE_REPLY))
                await emit(fallback_guard.flush())
                guard = fallback_guard

    meta = _build_meta(state, guard)
    await publish_reply_end(tenant_id, user_id, reply_to, meta)
    return "".join(chunks), meta
