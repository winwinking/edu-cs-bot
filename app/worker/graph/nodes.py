"""图节点（PHASE2.md 2.7 第 2、6 点）。

chitchat、sensitive、fallback 在这里；knowledge/finance/command/confirm_action/reminder
等业务节点都在各自的步骤（2.8~2.11、阶段三第 2 步）实现后放进了各自的模块文件
（knowledge.py/finance.py/command.py/handoff.py/reminder.py），不集中在这一个文件里。
"""
from sqlalchemy import select

from app.common.models import Tenant, User
from app.worker.graph.reminder import format_reminder_list_block, load_active_reminders
from app.worker.graph.state import GraphState
from app.worker.graph.style import (
    FALLBACK_INVALID_OUTPUT_REPLY,
    FALLBACK_LLM_UNAVAILABLE_REPLY,
    SENSITIVE_REPLY,
    STYLE_SYSTEM_PROMPT,
)


async def load_context(state: GraphState, runtime) -> dict:
    """身份只有一个来源：role 从数据库查，不能用 LLM 输出里的身份字段（CLAUDE.md 硬性规则）。

    顺带查出 tenant_timezone（提醒相对时间换算用）和当前生效中的提醒列表（阶段三第 2 步，
    classify 组装 LLM 请求时要用）——不管这条消息是不是在说提醒都会查一次，换一次小查询
    换来的是分类阶段随时能看到最新状态，不用先判断"这条消息像不像在说提醒"才决定要不要查。
    """
    session = runtime.context.session
    result = await session.execute(
        select(User.role, Tenant.timezone)
        .join(Tenant, Tenant.id == User.tenant_id)
        .where(User.tenant_id == state["tenant_id"], User.id == state["user_id"])
    )
    role, tenant_timezone = result.one()
    reminders = await load_active_reminders(session, state["tenant_id"], state["user_id"])
    return {
        "role": role.value,
        "tenant_timezone": tenant_timezone,
        "reminder_list_block": format_reminder_list_block(reminders),
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
