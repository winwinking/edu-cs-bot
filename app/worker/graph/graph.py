"""编图（PHASE2.md 2.7 第 2、4 点）：load_context -> classify -> 条件路由 -> 业务节点 -> END。

respond() 是图外面的统一出口：图只负责决策，产出 reply_plan，不直接往外发消息；respond() 拿到
跑完图的最终 state，负责把 reply_plan 转成真正的 reply_chunk/reply_end 发给客户端，template 按句
切分，generate 流式调 LLM 并经过 OutputGuard，最后统一发 reply_end（带 meta）。
"""
import time
from typing import Any

from langgraph.graph import END, StateGraph
from openai import APIConnectionError, APIError, APITimeoutError

from app.common.circuit_breaker import CircuitBreakerOpenError
from app.common.llm_client import stream_chat_completion
from app.common.logging import get_logger
from app.worker.graph.classify import classify
from app.worker.graph.command import cancel_action, command, confirm_action, confirm_ambiguous, request_confirmation
from app.worker.graph.finance import finance
from app.worker.graph.guard import OutputGuard
from app.worker.graph.handoff import dissatisfied_first, handoff
from app.worker.graph.knowledge import knowledge
from app.worker.graph.nodes import chitchat, fallback, load_context, sensitive
from app.worker.graph.reminder import reminder
from app.worker.graph.state import GraphContext, GraphState
from app.worker.graph.style import FALLBACK_LLM_UNAVAILABLE_REPLY
from app.worker.metrics import first_token_seconds
from app.worker.pubsub import publish_reply_chunk, publish_reply_end

logger = get_logger(__name__)

# 意图 -> 节点名。knowledge 已在 2.8、finance 已在 2.9、command/request_confirmation/
# confirm_action/cancel_action/confirm_ambiguous 已在 2.10、handoff/dissatisfied_first
# 已在 2.11、reminder 已在阶段三第 2 步接入真正的实现，所有业务节点全部落地
_INTENT_TO_NODE = {
    "knowledge_qa": "knowledge",
    "finance_query": "finance",
    "platform_command": "command",
    "reminder": "reminder",
    "chitchat": "chitchat",
    "handoff": "handoff",
    "dissatisfied_first": "dissatisfied_first",
    "confirm_action": "confirm_action",
    "cancel_action": "cancel_action",
    "confirm_ambiguous": "confirm_ambiguous",
    "fallback": "fallback",
}

_BUSINESS_NODES = (
    "knowledge",
    "finance",
    "command",
    "request_confirmation",
    "confirm_action",
    "cancel_action",
    "confirm_ambiguous",
    "reminder",
    "handoff",
    "dissatisfied_first",
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
    graph.add_node("knowledge", knowledge)
    graph.add_node("finance", finance)
    graph.add_node("command", command)
    graph.add_node("request_confirmation", request_confirmation)
    graph.add_node("confirm_action", confirm_action)
    graph.add_node("cancel_action", cancel_action)
    graph.add_node("confirm_ambiguous", confirm_ambiguous)
    graph.add_node("reminder", reminder)
    graph.add_node("handoff", handoff)
    graph.add_node("dissatisfied_first", dissatisfied_first)
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
        # 本轮 LLM 请求带了几条原文历史、有没有带历史摘要（阶段三第 3 步）
        "context": {
            "history_messages": len(state.get("history", [])),
            "has_summary": bool(state.get("history_summary")),
        },
        # 本轮因为熔断打开被跳过的服务（阶段三第 5 步），没有就是空列表
        "circuit_breaker": state.get("circuit_breaker", []),
    }


async def respond(tenant_id: str, user_id: str, reply_to: str, state: GraphState) -> tuple[str, dict[str, Any]]:
    plan = state["reply_plan"]
    # allowed_citations/lead_in 只有知识问答的 reply_plan 会带（2.8），其它节点不传就是 None，
    # OutputGuard 不做出处核对、不拼出处开头，行为跟 2.7 完全一样
    guard = OutputGuard(allowed_citations=plan.get("allowed_citations"), lead_in=plan.get("lead_in"))
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
        except CircuitBreakerOpenError:
            logger.warning("LLM 熔断打开，生成回复降级为固定话术")
            state["circuit_breaker"] = list(state.get("circuit_breaker", [])) + ["llm"]
            if not chunks:
                fallback_guard = OutputGuard()
                await emit(fallback_guard.feed(FALLBACK_LLM_UNAVAILABLE_REPLY))
                await emit(fallback_guard.flush())
                guard = fallback_guard
        except (APIError, APITimeoutError, APIConnectionError) as exc:
            logger.warning("生成回复时 LLM 调用失败", error=str(exc))
            if not chunks:
                # 一个分片都还没发出去，整体换成"LLM 不可用"固定话术；已经发出去的内容不回滚，
                # 保留目前的 guard 统计（不重新计一遍）
                fallback_guard = OutputGuard()
                await emit(fallback_guard.feed(FALLBACK_LLM_UNAVAILABLE_REPLY))
                await emit(fallback_guard.flush())
                guard = fallback_guard

        # 知识问答专用兜底（2.8 第 5 点）：LLM 生成的句子全被出处检查拦下了（或者干脆没生成出
        # 任何有效内容），guard 手上一句都没成功发出去——用排名第一的检索结果原文垫底，
        # 保证"依据……"这个出处开头后面一定跟着真实存在的内容，不会孤零零地漏发
        if plan.get("lead_in") and not guard.emitted_any and plan.get("fallback_text"):
            await emit(guard.feed(plan["fallback_text"]))
            await emit(guard.flush())

    meta = _build_meta(state, guard)
    await publish_reply_end(tenant_id, user_id, reply_to, meta)
    return "".join(chunks), meta
