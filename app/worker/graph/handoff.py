"""转人工（PHASE2.md 2.11）。

三条触发路径都在 classify.py 里判断（关键词/不满意计数到阈值/LLM 选了 transfer_to_human），
这里只管把转接记录建好、判断坐席在不在线。转接记录要给人工客服看，所以 summary 必须先过
mask_text() 脱敏（硬性规则：日志/审计不留敏感信息原文，转接记录本质上也是一种审计留痕）。
"""
import time
import uuid
from typing import Any, Optional

from openai import APIConnectionError, APIError, APITimeoutError
from sqlalchemy import func, select

from app.common.llm_client import LLM_MODEL, chat_completion, estimate_tokens, extract_usage_from_response
from app.common.llm_usage import add_tokens_used, get_daily_budget, is_budget_exceeded, record_llm_usage
from app.common.logging import get_logger
from app.common.masking import mask_text
from app.common.models import (
    AuditLog,
    HandoffStatus,
    HandoffTicket,
    HandoffTrigger,
    Message,
    MessageRole,
    PendingAction,
    PendingActionStatus,
    Tenant,
)
from app.common.platform_client import PlatformUnavailable, get_agents_status
from app.worker.graph.state import GraphState
from app.worker.graph.style import DISSATISFIED_FIRST_REPLY

logger = get_logger(__name__)

_HANDOFF_SUMMARY_PROMPT_PREFIX = (
    "转人工摘要：请根据下面这段最近的对话，写三句客观的中文摘要，说明用户的问题和处理情况，"
    "不要加评价或建议，不要编造对话里没有的内容。\n\n"
)


def _fallback_summary(messages: list[dict]) -> str:
    """LLM 摘要失败时的兜底（PHASE2.md 2.11 第 2 点）：拼最近 3 条用户消息，每条截断到 50 字。"""
    user_texts = [m["content"] for m in messages if m["role"] == "user"]
    return "；".join(text[:50] for text in user_texts[-3:])


async def _generate_summary(state: GraphState, session, timing_holder: Optional[dict] = None) -> str:
    """timing_holder：跟 respond() 的 usage_holder 一个用法（阶段三 3.8 第二轮）——这个函数不是
    图节点，调用方 handoff() 才是，函数自己没法把耗时写进节点返回值，只能通过调用方传进来的
    可变字典带出去。"""
    history = list(state.get("history", []))
    transcript_messages = history + [{"role": "user", "content": state["content"]}]
    transcript = "\n".join(
        f"{'用户' if m['role'] == 'user' else '客服'}：{m['content']}" for m in transcript_messages
    )

    tenant_id = state["tenant_id"]
    tenant_timezone = state.get("tenant_timezone") or "Asia/Shanghai"
    summary = ""
    # 预算用完时复用已有的兜底拼接（跟"LLM 调用失败"走同一条路径）：转人工摘要不是给用户看的
    # 正式回复，少一次 LLM 润色不影响转接本身，没必要为这一条单独设计新的降级文案
    budget = await get_daily_budget(session, tenant_id)
    if await is_budget_exceeded(tenant_id, tenant_timezone, budget):
        logger.info("机构今日 token 预算已用完，转人工摘要改用模板拼接", tenant_id=tenant_id)
    else:
        messages = [{"role": "user", "content": _HANDOFF_SUMMARY_PROMPT_PREFIX + transcript}]
        llm_start = time.monotonic()
        try:
            response = await chat_completion(messages=messages)
            summary = (response.choices[0].message.content or "").strip()
        except (APIError, APITimeoutError, APIConnectionError) as exc:
            logger.warning("生成转人工摘要失败，改用模板拼接", error=str(exc))
        finally:
            if timing_holder is not None:
                timing_holder["llm_ms"] = timing_holder.get("llm_ms", 0) + (time.monotonic() - llm_start) * 1000

        if summary:
            usage = extract_usage_from_response(response)
            estimated = usage is None
            prompt_tokens, completion_tokens = usage or estimate_tokens(messages, summary)
            await add_tokens_used(tenant_id, tenant_timezone, prompt_tokens + completion_tokens)
            await record_llm_usage(
                session,
                tenant_id=tenant_id,
                conversation_id=state.get("conversation_id"),
                trace_id=state.get("trace_id"),
                purpose="handoff",
                model=LLM_MODEL,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                estimated=estimated,
            )

    if not summary:
        summary = _fallback_summary(transcript_messages)

    # 摘要要给人工客服看，跟日志、审计一样不能带敏感信息原文（硬性规则）
    return mask_text(summary)


async def _get_service_hours(session, tenant_id: str) -> str:
    """转人工"不在线"话术里展示的服务时间，从 `tenants.service_hours` 读（阶段二 2.11 第 3 点），
    不写死在代码里——在不在线本身完全由 mock-platform 的 `/agents/status` 决定，这里只负责取
    展示文案，不参与判断，所以不会出现"mockctl.py 手动切成不在线，但取到的文案对不上租户"的问题。"""
    result = await session.execute(select(Tenant.service_hours).where(Tenant.id == tenant_id))
    row = result.first()
    return row[0] if row else "9:00 至 21:00"


def _build_online_reply(queue_length: int, avg_wait_minutes: int) -> str:
    return (
        f"已为你转接人工客服，前面还有 {queue_length} 位，预计 {avg_wait_minutes} 分钟接入。"
        "刚才的情况我已经同步给客服，不用再重复描述。"
    )


def _build_offline_reply(service_hours: str) -> str:
    return (
        f"人工客服现在不在线，服务时间是每天 {service_hours}。你可以直接在这里留言，"
        "我会连同刚才的情况一起转给客服，上班后优先回复你。"
    )


# handoff_tickets.intent 只给坐席看"用户在转人工之前实际想办的业务"是什么；handoff 本身和
# dissatisfied_first 都是转人工流程内部产生的状态（前者就是这次转人工这一步，后者是还没转人工
# 之前的道歉引导），不是业务意图，对坐席没有参考价值，两个都要排除（人审发现 dissatisfied_first
# 没排除，见 AGENT_LOG 步骤 2.11）
_NON_BUSINESS_INTENTS = ("handoff", "dissatisfied_first")


async def _last_business_intent(session, tenant_id: str, conversation_id: str) -> Optional[str]:
    result = await session.execute(
        select(Message.intent)
        .where(
            Message.tenant_id == tenant_id,
            Message.conversation_id == uuid.UUID(conversation_id),
            Message.role == MessageRole.assistant,
            Message.intent.isnot(None),
            Message.intent.notin_(_NON_BUSINESS_INTENTS),
        )
        .order_by(Message.created_at.desc())
        .limit(1)
    )
    row = result.first()
    return row[0] if row else None


async def _collect_attempted_actions(session, tenant_id: str, conversation_id: str) -> list[dict]:
    """最小版本：本会话的审计记录（AuditLog）和待确认操作（PendingAction）各按时间正序列出，
    每项带来源、动作、结果、时间——够坐席一眼看出这个会话之前都做过什么、结果如何。"""
    audit_result = await session.execute(
        select(AuditLog)
        .where(AuditLog.tenant_id == tenant_id, AuditLog.conversation_id == uuid.UUID(conversation_id))
        .order_by(AuditLog.created_at.asc())
    )
    pending_result = await session.execute(
        select(PendingAction)
        .where(PendingAction.tenant_id == tenant_id, PendingAction.conversation_id == uuid.UUID(conversation_id))
        .order_by(PendingAction.created_at.asc())
    )

    actions: list[dict] = []
    for row in audit_result.scalars().all():
        actions.append(
            {
                "source": "audit_log",
                "action": row.resource or row.action,
                "result": row.result,
                "created_at": row.created_at.isoformat(),
            }
        )
    for row in pending_result.scalars().all():
        actions.append(
            {
                "source": "pending_action",
                "action": row.args.get("action", row.tool_name),
                "result": row.status.value,
                "created_at": row.created_at.isoformat(),
            }
        )
    return actions


async def _collect_risk_flags(session, tenant_id: str, conversation_id: str, state: GraphState) -> list[str]:
    flags: list[str] = []
    if "prompt_injection_suspected" in state.get("risk_flags", []):
        flags.append("prompt_injection_suspected")
    if state.get("handoff_trigger") == "dissatisfied":
        flags.append("repeated_dissatisfaction")

    forbidden = await session.execute(
        select(AuditLog.id)
        .where(
            AuditLog.tenant_id == tenant_id,
            AuditLog.conversation_id == uuid.UUID(conversation_id),
            AuditLog.action == "query_finance",
            AuditLog.result == "forbidden",
        )
        .limit(1)
    )
    if forbidden.first() is not None:
        flags.append("finance_forbidden_attempt")

    pending = await session.execute(
        select(PendingAction.id)
        .where(
            PendingAction.tenant_id == tenant_id,
            PendingAction.conversation_id == uuid.UUID(conversation_id),
            PendingAction.status == PendingActionStatus.pending,
            PendingAction.expires_at > func.now(),
        )
        .limit(1)
    )
    if pending.first() is not None:
        flags.append("high_risk_pending")

    return flags


async def handoff(state: GraphState, runtime) -> dict[str, Any]:
    session = runtime.context.session
    tenant_id = state["tenant_id"]
    user_id = state["user_id"]
    conversation_id = state["conversation_id"]
    # classify.py 三条触发路径都会设 handoff_trigger；理论上不会缺失，缺失时按 keyword 兜底，
    # 不让转接这件事本身因为一个次要字段没设就失败
    trigger_value = state.get("handoff_trigger") or "keyword"

    llm_timing: dict = {}
    summary = await _generate_summary(state, session, llm_timing)
    intent = await _last_business_intent(session, tenant_id, conversation_id)
    attempted_actions = await _collect_attempted_actions(session, tenant_id, conversation_id)
    risk_flags = await _collect_risk_flags(session, tenant_id, conversation_id, state)

    try:
        agents = await get_agents_status()
        online = bool(agents.get("online"))
    except PlatformUnavailable as exc:
        # 超时/连接失败/上游 5xx 都会被 platform_client 统一包成这个异常；查不到坐席状态时
        # 保守按不在线处理，不能因为查询本身失败就假装在线、给用户一个排队号却没人接
        logger.warning("查询坐席状态失败，按不在线处理", error=str(exc))
        online = False
        agents = {}

    status = HandoffStatus.queued if online else HandoffStatus.left_message
    ticket_id = uuid.uuid4()
    session.add(
        HandoffTicket(
            id=ticket_id,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=uuid.UUID(conversation_id),
            trigger=HandoffTrigger(trigger_value),
            intent=intent,
            summary=summary,
            attempted_actions=attempted_actions,
            risk_flags=risk_flags,
            status=status,
        )
    )
    await session.commit()
    # PHASE4.md 4.6 人审发现同一批：只记状态/ID，summary 是脱敏过的摘要也不放进日志——
    # 摘要脱敏是为了给人工客服看，不代表可以随便出现在别的地方
    logger.info(
        "转人工工单创建",
        conversation_id=conversation_id,
        user_id=user_id,
        ticket_id=str(ticket_id),
        trigger=trigger_value,
        status=status.value,
        online=online,
    )

    if online:
        reply = _build_online_reply(agents.get("queue_length", 0), agents.get("avg_wait_minutes", 0))
    else:
        service_hours = await _get_service_hours(session, tenant_id)
        reply = _build_offline_reply(service_hours)

    return {
        "reply_plan": {"mode": "template", "text": reply},
        "handoff_ticket_id": str(ticket_id),
        "tools_meta": [{"name": "transfer_to_human", "status": "ok"}],
        "llm_ms": state.get("llm_ms", 0) + llm_timing.get("llm_ms", 0),
    }


async def dissatisfied_first(state: GraphState, runtime) -> dict[str, Any]:
    return {"reply_plan": {"mode": "template", "text": DISSATISFIED_FIRST_REPLY}}
