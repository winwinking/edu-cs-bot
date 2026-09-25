"""图节点（PHASE2.md 2.7 第 2、6 点）。

chitchat、sensitive、fallback 在这里；knowledge/finance/command/confirm_action/reminder
等业务节点都在各自的步骤（2.8~2.11、阶段三第 2 步）实现后放进了各自的模块文件
（knowledge.py/finance.py/command.py/handoff.py/reminder.py），不集中在这一个文件里。
"""
import uuid

from sqlalchemy import select

from app.common.llm_usage import get_daily_budget, is_budget_exceeded
from app.common.models import Tenant, User
from app.worker.graph.context_summary import append_summary_block, load_history_summary
from app.worker.graph.reminder import format_reminder_list_block, load_active_reminders
from app.worker.graph.state import GraphState
from app.worker.graph.style import (
    BUDGET_EXCEEDED_CHITCHAT_REPLY,
    FALLBACK_BUDGET_EXCEEDED_REPLY,
    FALLBACK_INVALID_OUTPUT_REPLY,
    FALLBACK_LLM_UNAVAILABLE_REPLY,
    SENSITIVE_REPLY,
    STYLE_SYSTEM_PROMPT,
)


async def load_context(state: GraphState, runtime) -> dict:
    """身份只有一个来源：role 从数据库查，不能用 LLM 输出里的身份字段（CLAUDE.md 硬性规则）。

    顺带查出 tenant_timezone（提醒相对时间换算用）、当前生效中的提醒列表（阶段三第 2 步）、
    这个会话的历史摘要（阶段三第 3 步）——不管这条消息具体是什么都会查一次，换几次小查询
    换来的是分类/生成阶段随时能看到最新状态，不用先判断"这条消息像不像需要这个信息"。
    """
    session = runtime.context.session
    result = await session.execute(
        select(User.role, Tenant.timezone)
        .join(Tenant, Tenant.id == User.tenant_id)
        .where(User.tenant_id == state["tenant_id"], User.id == state["user_id"])
    )
    role, tenant_timezone = result.one()
    reminders = await load_active_reminders(session, state["tenant_id"], state["user_id"])
    history_summary = await load_history_summary(
        session, state["tenant_id"], uuid.UUID(state["conversation_id"])
    )
    return {
        "role": role.value,
        "tenant_timezone": tenant_timezone,
        "reminder_list_block": format_reminder_list_block(reminders),
        "history_summary": history_summary,
        "risk_flags": list(state.get("risk_flags", [])),
        "citations": [],
        "tools_meta": [],
    }


async def chitchat(state: GraphState, runtime) -> dict:
    # token 预算检查放在这里（而不是 respond()）：这里有 session，判断结果直接决定
    # reply_plan 走 generate 还是 template，respond() 不用关心"这次要不要真的调 LLM"
    # 这层业务判断（阶段三第 6 步，设计决定 13）
    tenant_id = state["tenant_id"]
    tenant_timezone = state.get("tenant_timezone") or "Asia/Shanghai"
    budget = await get_daily_budget(runtime.context.session, tenant_id)
    if await is_budget_exceeded(tenant_id, tenant_timezone, budget):
        return {
            "reply_plan": {"mode": "template", "text": BUDGET_EXCEEDED_CHITCHAT_REPLY},
            "budget_exceeded": True,
        }

    user_content = append_summary_block(state["content"], state.get("history_summary"))
    messages = (
        [{"role": "system", "content": STYLE_SYSTEM_PROMPT}]
        + list(state.get("history", []))
        + [{"role": "user", "content": user_content}]
    )
    return {"reply_plan": {"mode": "generate", "messages": messages, "citations": [], "allowed_citations": []}}


async def sensitive(state: GraphState, runtime) -> dict:
    return {"reply_plan": {"mode": "template", "text": SENSITIVE_REPLY}}


# fallback_reason -> 话术：llm_unavailable 是"LLM 调用失败/熔断打开"（过一会儿再试可能好），
# budget_exceeded 是"机构今日预算用完"（要等第二天，不能说"稍后再试"，见人审记录），
# 其余（invalid_output）用兜底文案
_FALLBACK_REASON_TO_REPLY = {
    "llm_unavailable": FALLBACK_LLM_UNAVAILABLE_REPLY,
    "budget_exceeded": FALLBACK_BUDGET_EXCEEDED_REPLY,
}


async def fallback(state: GraphState, runtime) -> dict:
    reason = state.get("fallback_reason")
    text = _FALLBACK_REASON_TO_REPLY.get(reason, FALLBACK_INVALID_OUTPUT_REPLY)
    return {"reply_plan": {"mode": "template", "text": text}}
