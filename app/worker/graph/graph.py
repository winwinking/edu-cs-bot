"""编图（PHASE2.md 2.7 第 2、4 点）：load_context -> classify -> 条件路由 -> 业务节点 -> END。

respond() 是图外面的统一出口：图只负责决策，产出 reply_plan，不直接往外发消息；respond() 拿到
跑完图的最终 state，负责把 reply_plan 转成真正的 reply_chunk/reply_end 发给客户端，template 按句
切分，generate 流式调 LLM 并经过 OutputGuard，最后统一发 reply_end（带 meta）。
"""
import socket
import time
from typing import Any, Awaitable, Callable

from langgraph.graph import END, StateGraph
from openai import APIConnectionError, APIError, APITimeoutError

from app.common.circuit_breaker import CircuitBreakerOpenError
from app.common.db import AsyncSessionLocal
from app.common.llm_client import LLM_MODEL, estimate_tokens, stream_chat_completion
from app.common.llm_usage import add_tokens_used, record_llm_usage
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
from app.worker.metrics import first_token_seconds, reply_seconds, tool_calls_total
from app.worker.pubsub import publish_reply_chunk, publish_reply_end

logger = get_logger(__name__)

# 容器主机名当 worker 实例的标识（阶段三 3.8 第 9 点，演示控制台流程视图要显示"这条回复是哪个
# worker 处理的"）：多个 worker 副本各自的容器 hostname 天然不同，不用额外生成/维护一个实例 id
_WORKER_ID = socket.gethostname()

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


_NodeFn = Callable[[GraphState, Any], Awaitable[dict]]


def _timed(name: str, fn: _NodeFn) -> _NodeFn:
    """给节点打点用（阶段三 3.8 第二轮，Jo 批准的第二处后端改动）：只包一层计时，不碰节点自己
    的业务逻辑和返回值内容，把"这条消息实际经过了哪些节点、每个节点花了多久"如实记下来，供
    演示控制台的流程图回放用，也是阶段四拆分"系统耗时/mock 耗时"的数据来源之一（respond() 里
    LLM 调用的耗时单独记在 llm_ms，不在这里）。path/timings 用"读旧值、拼新值"的写法而不是
    直接改 state，是因为 LangGraph 只认节点返回值里的字段来更新 state，节点入参这个 state
    对象本身的原地修改不保证会被采纳（respond() 是图跑完之后才执行的普通函数，不受这条限制，
    所以那边可以直接对 state 赋值）。
    """

    async def wrapper(state: GraphState, runtime: Any) -> dict:
        start = time.monotonic()
        result = await fn(state, runtime)
        elapsed_ms = round((time.monotonic() - start) * 1000, 1)
        path = list(state.get("path", [])) + [name]
        timings = {**state.get("timings", {}), name: elapsed_ms}
        return {**result, "path": path, "timings": timings}

    return wrapper


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

    graph.add_node("load_context", _timed("load_context", load_context))
    graph.add_node("classify", _timed("classify", classify))
    graph.add_node("knowledge", _timed("knowledge", knowledge))
    graph.add_node("finance", _timed("finance", finance))
    graph.add_node("command", _timed("command", command))
    graph.add_node("request_confirmation", _timed("request_confirmation", request_confirmation))
    graph.add_node("confirm_action", _timed("confirm_action", confirm_action))
    graph.add_node("cancel_action", _timed("cancel_action", cancel_action))
    graph.add_node("confirm_ambiguous", _timed("confirm_ambiguous", confirm_ambiguous))
    graph.add_node("reminder", _timed("reminder", reminder))
    graph.add_node("handoff", _timed("handoff", handoff))
    graph.add_node("dissatisfied_first", _timed("dissatisfied_first", dissatisfied_first))
    graph.add_node("chitchat", _timed("chitchat", chitchat))
    graph.add_node("sensitive", _timed("sensitive", sensitive))
    graph.add_node("fallback", _timed("fallback", fallback))

    graph.set_entry_point("load_context")
    graph.add_edge("load_context", "classify")
    graph.add_conditional_edges("classify", _route, {name: name for name in _BUSINESS_NODES})
    for name in _BUSINESS_NODES:
        graph.add_edge(name, END)

    return graph.compile()


COMPILED_GRAPH = _build_graph()


def _build_meta(state: GraphState, guard: OutputGuard) -> dict[str, Any]:
    # 工具调用计数（阶段三第 6 步）：集中在这一个地方按 tools_meta 打点，不用在每个业务节点
    # （knowledge/finance/command/reminder/handoff）各自调一遍这个 Counter
    for tool in state.get("tools_meta", []):
        tool_calls_total.labels(tool=tool["name"], status=tool["status"]).inc()

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
        # 本轮因为机构今日 token 预算用完而跳过 LLM 调用（阶段三第 6 步，设计决定 13）
        "budget_exceeded": state.get("budget_exceeded", False),
        # 这条回复是哪个 worker 副本处理的（阶段三 3.8 第 9 点）
        "worker_id": _WORKER_ID,
        # 这条消息实际经过的图节点名，按执行顺序（阶段三 3.8 第二轮，演示控制台流程图回放用）；
        # respond() 不是 StateGraph 的节点，是图跑完之后才调用的普通函数，在 respond() 末尾
        # 手工追加，不是 _timed() 打的点
        "path": state.get("path", []),
        # 每个节点自己的耗时（毫秒），键是节点名，取值来自 path 同一份打点
        "timings": state.get("timings", {}),
        # 这条消息里真正花在等 LLM 网络调用上的时间（毫秒），累加自 classify/handoff/respond
        # 三处可能调 LLM 的地方，没调用 LLM 的请求这里是 0，不是 None——方便前端直接做除法/占比
        "llm_ms": round(state.get("llm_ms", 0), 1),
    }


# reply_plan 走 generate 模式的节点只有这两个（chitchat/knowledge），预算检查已经在各自节点里
# 做完了——respond() 只要能走到这个分支，就说明这次是真的要调 LLM，用 intent 反推 purpose，
# 记 token 用量的时候不用每个节点各自传一遍
_USAGE_PURPOSE_BY_INTENT = {"chitchat": "chat", "knowledge_qa": "knowledge"}


async def _record_generate_usage(state: GraphState, plan: dict, chunks: list[str], usage_holder: dict) -> None:
    tenant_id = state["tenant_id"]
    tenant_timezone = state.get("tenant_timezone") or "Asia/Shanghai"
    purpose = _USAGE_PURPOSE_BY_INTENT.get(state.get("intent"), "chat")

    if usage_holder:
        prompt_tokens = usage_holder["prompt_tokens"]
        completion_tokens = usage_holder["completion_tokens"]
        estimated = False
    else:
        # mock-llm/真实 LLM 没吐出 usage（提供方不支持，或者中途报错没能收到最后那个 usage
        # chunk）时按字数估算，标 estimated=True，不能当精确成本汇总
        prompt_tokens, completion_tokens = estimate_tokens(plan["messages"], "".join(chunks))
        estimated = True

    await add_tokens_used(tenant_id, tenant_timezone, prompt_tokens + completion_tokens)
    # respond() 故意不占用跑图时那个 session（见 app/worker/handler.py 的注释：生成阶段可能
    # 耗时较久，不该一直攥着一个数据库连接），这里只在生成结束之后临时开一个短连接写这一行记录
    async with AsyncSessionLocal() as session:
        await record_llm_usage(
            session,
            tenant_id=tenant_id,
            conversation_id=state.get("conversation_id"),
            trace_id=state.get("trace_id"),
            purpose=purpose,
            model=LLM_MODEL,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            estimated=estimated,
        )


async def respond(tenant_id: str, user_id: str, reply_to: str, state: GraphState) -> tuple[str, dict[str, Any]]:
    reply_start = time.monotonic()
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
        usage_holder: dict[str, int] = {}
        stream_ok = False
        try:
            async for delta in stream_chat_completion(plan["messages"], usage_holder=usage_holder):
                if not first_token_seen:
                    first_token_seconds.observe(time.monotonic() - start)
                    first_token_seen = True
                await emit(guard.feed(delta))
            await emit(guard.flush())
            stream_ok = True
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
        finally:
            # 熔断打开时这段耗时接近 0（熔断在真正发请求前就拦下了），不是"等 LLM"的时间，
            # 但为了不用再判断一次分支，还是按实际经过的时间记——影响小到可以忽略，不值得
            # 为了这点精度多写一层判断
            state["llm_ms"] = state.get("llm_ms", 0) + (time.monotonic() - start) * 1000

        # 知识问答专用兜底（2.8 第 5 点）：LLM 生成的句子全被出处检查拦下了（或者干脆没生成出
        # 任何有效内容），guard 手上一句都没成功发出去——用排名第一的检索结果原文垫底，
        # 保证"依据……"这个出处开头后面一定跟着真实存在的内容，不会孤零零地漏发
        fallback_used = bool(plan.get("lead_in") and not guard.emitted_any and plan.get("fallback_text"))
        if fallback_used:
            await emit(guard.feed(plan["fallback_text"]))
            await emit(guard.flush())

        # PHASE4.md 4.6 人审发现的同一处补充：只有知识问答的 reply_plan 才会带非空的
        # allowed_citations（chitchat 的 reply_plan 也带了这个字段，但值是空列表，不是
        # None——这里必须用真值判断，不能用"is not None"，不然会把 chitchat 也误判成知识问答
        # 记一条同名日志，见 AGENT_LOG 本步骤的记录），这里把 OutputGuard 真正核对出处的结果
        # 落一条结构化日志——删了几句、被删句子引用的是哪些编造出处、有没有触发上面这条
        # "输出第一条条款原文"兜底，只记编号和数量，不记生成的句子原文（脱敏规则管不到"业务上
        # 正常但不该被记录"的整段回复内容）
        if plan.get("allowed_citations"):
            logger.info(
                "知识问答 OutputGuard 核对完成",
                conversation_id=state.get("conversation_id"),
                dropped_sentences=guard.dropped_sentences,
                dropped_citations=[list(c) for c in guard.dropped_citations],
                banned_phrases_removed=guard.banned_phrases_removed,
                fallback_used=fallback_used,
            )

        # 只有真的成功调用了 LLM（没被熔断拦下、没有中途报错）才记账——被熔断拦下/调用失败的
        # 这次请求，mock-llm/真实 LLM 根本没收到或者没处理完，没有真实成本可记
        if stream_ok:
            await _record_generate_usage(state, plan, chunks, usage_holder)

    # respond() 不是 StateGraph 节点，_timed() 包不到它，这里手工补最后一段到 path/timings——
    # 图里最后一个业务节点只是"决定回复内容"，真正的生成/发送（含流式调 LLM、OutputGuard、
    # 发 reply_chunk）是这个函数做的，流程图回放要能看到这一步，不能漏掉
    state["path"] = list(state.get("path", [])) + ["respond"]
    state["timings"] = {**state.get("timings", {}), "respond": round((time.monotonic() - reply_start) * 1000, 1)}

    meta = _build_meta(state, guard)
    await publish_reply_end(tenant_id, user_id, reply_to, meta)
    reply_seconds.observe(time.monotonic() - reply_start)
    return "".join(chunks), meta
