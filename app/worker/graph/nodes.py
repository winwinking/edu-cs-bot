"""图节点（PHASE2.md 2.7 第 2、6 点）。

本步只把 chitchat、sensitive、fallback、reminder_stub 做成真正的行为；knowledge、finance、
command、handoff、request_confirmation、confirm_action、cancel_action 先统一用 `placeholder`
产出占位回复，阶段二后续步骤（2.8~2.11）逐个替换成真正的实现，不提前实现还没到的业务节点。
"""
from sqlalchemy import select

from app.common.models import User
from app.worker.graph.state import GraphState
from app.worker.graph.style import (
    FALLBACK_INVALID_OUTPUT_REPLY,
    FALLBACK_LLM_UNAVAILABLE_REPLY,
    PLACEHOLDER_REPLY,
    REMINDER_STUB_REPLY,
    SENSITIVE_REPLY,
    STYLE_SYSTEM_PROMPT,
)


async def load_context(state: GraphState, runtime) -> dict:
    """身份只有一个来源：role 从数据库查，不能用 LLM 输出里的身份字段（CLAUDE.md 硬性规则）。"""
    session = runtime.context.session
    result = await session.execute(
        select(User.role).where(User.tenant_id == state["tenant_id"], User.id == state["user_id"])
    )
    role = result.scalar_one()
    return {
        "role": role.value,
        "risk_flags": list(state.get("risk_flags", [])),
        "citations": [],
        "tools_meta": [],
    }


async def chitchat(state: GraphState, runtime) -> dict:
    messages = (
        [{"role": "system", "content": STYLE_SYSTEM_PROMPT}]
        + list(state.get("history", []))
        + [{"role": "user", "content": state["content"]}]
    )
    return {"reply_plan": {"mode": "generate", "messages": messages, "citations": [], "allowed_citations": []}}


async def sensitive(state: GraphState, runtime) -> dict:
    return {"reply_plan": {"mode": "template", "text": SENSITIVE_REPLY}}


async def fallback(state: GraphState, runtime) -> dict:
    reason = state.get("fallback_reason")
    text = FALLBACK_LLM_UNAVAILABLE_REPLY if reason == "llm_unavailable" else FALLBACK_INVALID_OUTPUT_REPLY
    return {"reply_plan": {"mode": "template", "text": text}}


async def reminder_stub(state: GraphState, runtime) -> dict:
    return {"reply_plan": {"mode": "template", "text": REMINDER_STUB_REPLY}}


async def placeholder(state: GraphState, runtime) -> dict:
    return {"reply_plan": {"mode": "template", "text": PLACEHOLDER_REPLY}}
