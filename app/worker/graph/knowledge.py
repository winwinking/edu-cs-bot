"""知识问答节点（PHASE2.md 2.8）。

无命中直接回固定话术，不给 LLM 编的机会；命中时把资料放进 user 消息的 <资料> 块（不进
system prompt），出处开头由代码生成，LLM 生成的内容经 OutputGuard 逐句核对出处。
"""
from typing import List, Optional

import httpx

from app.common.llm_usage import get_daily_budget, is_budget_exceeded
from app.common.logging import get_logger
from app.common.prompt_guard import build_reference_block
from app.common.retrieval import SearchResult, get_retriever
from app.worker.graph.context_summary import append_summary_block
from app.worker.graph.state import GraphState
from app.worker.graph.style import KNOWLEDGE_NO_HIT_REPLY, KNOWLEDGE_SYSTEM_ADDENDUM, STYLE_SYSTEM_PROMPT

logger = get_logger(__name__)

# 触发"拼上上一条用户问题再检索"的条件（PHASE2.md 2.8 第 1 点）
_REWRITE_MIN_LENGTH = 8
_FOLLOWUP_WORDS = ("那", "呢")

# 出处开头最多列几条：太多条挤在开头一句话里不好读，只取排名靠前、真正超过阈值的
_MAX_LEAD_IN_CITATIONS = 2


def _last_user_message(history: List[dict]) -> Optional[str]:
    for msg in reversed(history):
        if msg.get("role") == "user":
            return msg.get("content")
    return None


def _rewrite_query(content: str, history: List[dict]) -> str:
    needs_rewrite = len(content) < _REWRITE_MIN_LENGTH or any(word in content for word in _FOLLOWUP_WORDS)
    if not needs_rewrite:
        return content
    prev = _last_user_message(history)
    if not prev:
        return content
    # 当前问题在哈希向量里要比上一条问题权重更高，不然"常规班/寒假班"这类上一轮提到、但跟这一轮
    # 追问的具体对象无关的词，会因为字面重合度更高把检索结果带偏。用重复当前问题一次来提高它的
    # 字符/双字组计数权重——这是通用的加权手段，不针对任何具体词，对所有"少于 8 字或含那/呢"的
    # 改写场景都适用
    return f"{prev}{content}{content}"


def _build_lead_in(citations: List[SearchResult]) -> str:
    parts = [f"《{c.doc_title}》第 {c.clause_no} 条" for c in citations]
    return "依据" + "、".join(parts) + "："


async def knowledge(state: GraphState, runtime) -> dict:
    query = _rewrite_query(state["content"], state.get("history", []))
    retriever = get_retriever()

    try:
        results = await retriever.search(state["tenant_id"], query, top_k=3)
        tool_status = "ok"
    except httpx.TimeoutException:
        # 只有 MockKnowledgeRetriever 会走到这条（调用 mock-knowledge，带 2 秒超时）
        logger.warning("知识检索超时，按无命中处理", conversation_id=state.get("conversation_id"), query=query)
        results, tool_status = [], "timeout"
    except Exception as exc:  # noqa: BLE001 —— 检索失败不能让整条流水线崩掉，降级成"没查到"
        logger.warning(
            "知识检索失败，按无命中处理", conversation_id=state.get("conversation_id"), query=query, error=str(exc)
        )
        results, tool_status = [], "upstream_error"

    # 阈值是检索器自己的属性，不是全局配置——pgvector 和 mock_knowledge 打分尺度不一样（见 2.3）
    qualifying = [r for r in results if r.score >= retriever.min_score]

    # PHASE4.md 4.6 人审发现：故障注入时靠 trace_id 在 worker 日志里搜不到知识问答路径的任何
    # 一行结构化日志，没法确认这次检索用的是什么 query、命中了哪些条款、有没有低于阈值。这里
    # 只记条款编号和分数，不记条款原文/回复原文——日志脱敏规则不认识"知识条款内容"这个字段名，
    # 记全文有把知识库内容原样堆进日志的风险，而条款编号本身就足够排查检索结果对不对
    logger.info(
        "知识检索完成",
        conversation_id=state.get("conversation_id"),
        query=query,
        min_score=retriever.min_score,
        tool_status=tool_status,
        results=[
            {
                "doc_title": r.doc_title,
                "clause_no": r.clause_no,
                "score": round(r.score, 4),
                "below_threshold": r.score < retriever.min_score,
            }
            for r in results
        ],
    )

    if not qualifying:
        return {
            "reply_plan": {"mode": "template", "text": KNOWLEDGE_NO_HIT_REPLY},
            "citations": [],
            "tools_meta": [{"name": "search_knowledge", "status": tool_status}],
        }

    lead_in = _build_lead_in(qualifying[:_MAX_LEAD_IN_CITATIONS])
    citations_meta = [
        {"doc_title": c.doc_title, "clause_no": c.clause_no, "score": round(c.score, 4)} for c in qualifying
    ]

    # token 预算用完时不调 LLM 组织语言，直接把命中条款原文带出处发出去（PHASE3.md 第 6 步，
    # 设计决定 13：知识问答"直接给出命中条款原文并带出处"）——出处、条款内容都来自检索结果，
    # 不是编的，这条路径本来就比 LLM 生成更"保真"，只是少了 LLM 把多条资料揉成一段话的润色
    budget = await get_daily_budget(runtime.context.session, state["tenant_id"])
    if await is_budget_exceeded(state["tenant_id"], state.get("tenant_timezone") or "Asia/Shanghai", budget):
        return {
            "reply_plan": {"mode": "template", "text": f"{lead_in}{qualifying[0].content}"},
            "citations": citations_meta,
            "tools_meta": [{"name": "search_knowledge", "status": tool_status}],
            "budget_exceeded": True,
        }

    snippets = [f"《{c.doc_title}》第 {c.clause_no} 条\n{c.content}" for c in qualifying]
    reference_block = build_reference_block(snippets)

    user_content = append_summary_block(state["content"], state.get("history_summary"))
    messages = (
        [{"role": "system", "content": f"{STYLE_SYSTEM_PROMPT}\n\n{KNOWLEDGE_SYSTEM_ADDENDUM}"}]
        + list(state.get("history", []))
        + [{"role": "user", "content": f"{reference_block}\n\n{user_content}"}]
    )

    return {
        "reply_plan": {
            "mode": "generate",
            "messages": messages,
            # respond() 直接用这两个字段驱动 OutputGuard 的出处核对和"全部丢掉"兜底，
            # 不用反过来从 citations 猜格式
            "allowed_citations": [[c.doc_title, c.clause_no] for c in qualifying],
            "lead_in": lead_in,
            "fallback_text": f"我查到的相关规定是：{qualifying[0].content}",
        },
        # meta.citations 给客户端/调试用，不带正文内容，只带出处和分数
        "citations": citations_meta,
        "tools_meta": [{"name": "search_knowledge", "status": tool_status}],
    }
