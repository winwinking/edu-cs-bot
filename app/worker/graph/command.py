"""平台指令与二次确认（PHASE2.md 2.10）。

低风险指令（打开课程表、查学习报告、修改课程提醒）由 `command` 节点直接执行。高风险指令
（关闭/开通自动续费、请假）先由 `request_confirmation` 生成待确认操作，用户回复"确认……"
由 `confirm_action` 原子抢占执行，回复"取消/算了/不用了"由 `cancel_action` 撤销；回复"对/是的"
这类不含"确认"字样的短句由 `confirm_ambiguous` 提醒用户要回复确切的确认短语。

抢占用数据库原子 UPDATE（status='pending' AND expires_at>now() 才能抢到），不是先 SELECT
再 UPDATE——并发场景下"先查后改"两步之间状态可能已经变了，原子 UPDATE 把"判断"和"占用"合成
一步，用户手快连发两次"确认关闭"也只有一次能抢到。
"""
import uuid
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select, update

from app.common.config import get_settings
from app.common.logging import get_logger
from app.common.models import AuditLog, PendingAction, PendingActionStatus, UserRole
from app.common.platform_client import PlatformUnavailable, get_subscriptions, submit_command
from app.worker.graph.state import GraphState
from app.worker.graph.style import (
    FALLBACK_LLM_UNAVAILABLE_REPLY,
    PLATFORM_ALREADY_PROCESSED_REPLY,
    PLATFORM_CONFIRM_TIMEOUT_REPLY,
    PLATFORM_LOW_RISK_ERROR_REPLY,
)

logger = get_logger(__name__)
settings = get_settings()

_LOW_RISK_ACTIONS = frozenset({"open_schedule", "query_study_report", "update_course_reminder"})

_LOW_RISK_REPLY = {
    "open_schedule": "已经为你打开课程表，可以在小程序里查看完整安排。",
    "query_study_report": "已经帮你生成了最新的学习报告，可以在小程序里查看。",
    "update_course_reminder": "已经帮你更新了课程提醒设置。",
}

# 动作 -> 确认短语 / 取消后状态说明 / 失败话术里的动词，三张表按 action 驱动文案，
# 不写死具体课程名这类跟场景绑定的内容（那些由调用方按实际数据拼进模板）
_CONFIRM_PHRASE = {"disable_auto_renew": "确认关闭", "enable_auto_renew": "确认开通", "submit_leave": "确认提交"}
_FAIL_VERB = {"disable_auto_renew": "关闭", "enable_auto_renew": "开通", "submit_leave": "提交"}


def _cancel_state_text(action: str, args: dict) -> str:
    if action == "disable_auto_renew":
        return "自动续费保持开启"
    if action == "enable_auto_renew":
        return "自动续费保持关闭"
    if action == "submit_leave":
        return "请假没有提交，课程照常安排"
    return "当前状态保持不变"


def _confirm_phrase(action: str) -> str:
    return _CONFIRM_PHRASE.get(action, "确认")


def _format_date_cn(date_str: Optional[str]) -> str:
    if not date_str:
        return ""
    d = date_cls.fromisoformat(date_str)
    return f"{d.month}月{d.day}日"


def _build_confirm_text(action: str, args: dict) -> str:
    if action == "disable_auto_renew":
        return (
            f"我先确认一下：你要关闭的是“{args['course_name']}”的自动续费，对吗？"
            "关闭后不影响已购课程，本月已排课程照常上。回复“确认关闭”我就处理。"
        )
    if action == "enable_auto_renew":
        return (
            f"我先确认一下：你要开通的是“{args['course_name']}”的自动续费，对吗？"
            "开通后到期会自动扣款续费。回复“确认开通”我就处理。"
        )
    if action == "submit_leave":
        course_part = f"“{args['course_name']}”" if args.get("course_name") else ""
        date_part = _format_date_cn(args.get("date")) or "这天"
        return (
            f"我先确认一下：你要请假的是{course_part}{date_part}的课，对吗？"
            "请假后这节课不计课时费。回复“确认提交”我就处理。"
        )
    return "我先确认一下这个操作，请回复“确认”我就处理。"


def _build_success_reply(action: str, args: dict) -> str:
    if action == "disable_auto_renew":
        return (
            f"已关闭“{args['course_name']}”的自动续费。本月已排课程照常上，"
            "下一期不会再自动扣款，需要重新开通随时告诉我。"
        )
    if action == "enable_auto_renew":
        return f"已开通“{args['course_name']}”的自动续费，到期会自动扣款续费。"
    if action == "submit_leave":
        course_part = f"“{args['course_name']}”" if args.get("course_name") else "这节课"
        date_part = _format_date_cn(args.get("date")) or "这天"
        return f"已为你提交{date_part}{course_part}的请假申请，不计课时费。"
    return "已经处理好了。"


def _build_failure_reply(action: str) -> str:
    verb = _FAIL_VERB.get(action, "处理")
    return f"这次没有{verb}成功，我已记录。你可以稍后再试，或者回复“转人工”。"


def _resolve_toggle_course(
    subscriptions: list[dict], course_name_arg: Optional[str], want_auto_renew: bool
) -> tuple[str, Any]:
    """want_auto_renew=False 表示要关闭（找当前开着的课）；True 表示要开通（找当前关着的课）。

    返回 (outcome, payload)：
    - "ok", course_name：能确定唯一一门课，可以生成待确认操作
    - "already", course_name：用户点名的课已经是目标状态，不用操作
    - "not_found", None：用户点名的课不在订阅列表里
    - "none", None：没有任何一门课处于"可以被切换"的状态
    - "ambiguous", [course_name, ...]：有不止一门课符合条件，要用户选
    """
    target_state = not want_auto_renew
    if course_name_arg:
        match = next((c for c in subscriptions if c["course_name"] == course_name_arg), None)
        if match is None:
            return "not_found", None
        if match["auto_renew"] == want_auto_renew:
            return "already", match["course_name"]
        return "ok", match["course_name"]

    candidates = [c["course_name"] for c in subscriptions if c["auto_renew"] == target_state]
    if not candidates:
        return "none", None
    if len(candidates) > 1:
        return "ambiguous", candidates
    return "ok", candidates[0]


async def _find_target_pending_action(session, tenant_id: str, conversation_id: str) -> Optional[PendingAction]:
    """confirm_action/cancel_action/confirm_ambiguous 共用：取这个会话最近一条待确认操作
    （不管状态），交给调用方按状态判断怎么处理——正常情况下一个会话同时只有一条 pending，
    取最近一条就是用户这句"确认/取消"要作用的目标。"""
    result = await session.execute(
        select(PendingAction)
        .where(PendingAction.tenant_id == tenant_id, PendingAction.conversation_id == uuid.UUID(conversation_id))
        .order_by(PendingAction.created_at.desc())
        .limit(1)
    )
    return result.scalars().first()


async def _write_platform_audit(
    session,
    *,
    tenant_id: str,
    actor_user_id: str,
    actor_role: UserRole,
    action: str,
    result: str,
    trace_id: Optional[str],
    conversation_id: Optional[str],
) -> None:
    session.add(
        AuditLog(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            actor_role=actor_role,
            action="platform_command",
            # 平台指令只作用于发起人自己的账号，没有"查别人"这个维度，target 就是发起人自己
            target_user_id=actor_user_id,
            resource=f"platform:{action}",
            result=result,
            trace_id=trace_id,
            conversation_id=uuid.UUID(conversation_id) if conversation_id else None,
            detail={"action": action},
        )
    )
    await session.commit()


async def command(state: GraphState, runtime) -> dict[str, Any]:
    """低风险指令：不用二次确认，直接执行。"""
    session = runtime.context.session
    tool_call = state.get("tool_call")
    if not tool_call or tool_call.get("name") != "platform_command":
        return {
            "reply_plan": {"mode": "template", "text": FALLBACK_LLM_UNAVAILABLE_REPLY},
            "tools_meta": [{"name": "platform_command", "status": "invalid_json"}],
        }

    args = tool_call["args"]
    action = args["action"]
    tenant_id = state["tenant_id"]
    user_id = state["user_id"]
    # 幂等键按题目要求 = {tenant_id}:{message_id}:{action}：同一条消息处理两次（比如 gateway
    # 侧重试投递），不会把"打开课程表"这类指令又对 mock-platform 执行第二次
    idempotency_key = f"{tenant_id}:{state['message_id']}:{action}"
    params = {k: v for k, v in args.items() if k != "action" and v is not None}

    try:
        response = await submit_command(
            tenant_id=tenant_id, user_id=user_id, action=action, params=params, idempotency_key=idempotency_key
        )
        success = response.get("status") == "success"
    except PlatformUnavailable as exc:
        logger.warning("低风险平台指令调用失败", action=action, error=str(exc))
        success = False

    await _write_platform_audit(
        session,
        tenant_id=tenant_id,
        actor_user_id=user_id,
        actor_role=UserRole(state["role"]),
        action=action,
        result="success" if success else "upstream_error",
        trace_id=state.get("trace_id"),
        conversation_id=state.get("conversation_id"),
    )

    reply = _LOW_RISK_REPLY.get(action, "已经处理好了。") if success else PLATFORM_LOW_RISK_ERROR_REPLY
    return {
        "reply_plan": {"mode": "template", "text": reply},
        "tools_meta": [{"name": "platform_command", "status": "ok" if success else "upstream_error"}],
    }


async def request_confirmation(state: GraphState, runtime) -> dict[str, Any]:
    """高风险指令：先生成待确认操作，不直接执行。"""
    session = runtime.context.session
    tool_call = state.get("tool_call")
    if not tool_call or tool_call.get("name") != "platform_command":
        return {
            "reply_plan": {"mode": "template", "text": FALLBACK_LLM_UNAVAILABLE_REPLY},
            "tools_meta": [{"name": "platform_command", "status": "invalid_json"}],
        }

    args = tool_call["args"]
    action = args["action"]
    tenant_id = state["tenant_id"]
    user_id = state["user_id"]
    conversation_id = state["conversation_id"]

    existing = await _find_target_pending_action(session, tenant_id, conversation_id)
    now = datetime.now(timezone.utc)
    if (
        existing is not None
        and existing.status == PendingActionStatus.pending
        and existing.expires_at > now
        and existing.args.get("action") == action
    ):
        # 同一会话同一个动作已有未过期的待确认，直接复用，不重复创建
        return {
            "reply_plan": {"mode": "template", "text": existing.confirm_text},
            "pending_action_id": str(existing.id),
            "tools_meta": [{"name": "platform_command", "status": "pending_confirmation"}],
        }

    if action in ("disable_auto_renew", "enable_auto_renew"):
        subscriptions = await get_subscriptions(tenant_id, user_id)
        want_auto_renew = action == "enable_auto_renew"
        outcome, payload = _resolve_toggle_course(subscriptions, args.get("course_name"), want_auto_renew)

        if outcome == "not_found":
            return {
                "reply_plan": {
                    "mode": "template",
                    "text": f"没有查到你名下叫“{args.get('course_name')}”的课程，麻烦确认一下课程名称。",
                },
                "tools_meta": [{"name": "platform_command", "status": "not_found"}],
            }
        if outcome == "already":
            state_word = "开启" if want_auto_renew else "关闭"
            return {
                "reply_plan": {"mode": "template", "text": f"“{payload}”目前已经是{state_word}状态，不用重复操作。"},
                "tools_meta": [{"name": "platform_command", "status": "no_op"}],
            }
        if outcome == "none":
            state_word = "开启" if want_auto_renew else "关闭"
            return {
                "reply_plan": {"mode": "template", "text": f"你名下的课程目前都已经{state_word}，不用做任何操作。"},
                "tools_meta": [{"name": "platform_command", "status": "no_op"}],
            }
        if outcome == "ambiguous":
            names = "、".join(f"“{n}”" for n in payload)
            verb = "开通" if want_auto_renew else "关闭"
            return {
                "reply_plan": {"mode": "template", "text": f"你名下这几门课都符合条件：{names}，需要{verb}哪一门？"},
                "tools_meta": [{"name": "platform_command", "status": "need_clarification"}],
            }
        confirm_args = {"action": action, "course_name": payload}
    elif action == "submit_leave":
        confirm_args = {
            "action": action,
            "course_name": args.get("course_name"),
            "date": args.get("date"),
            "reason": args.get("reason"),
        }
    else:
        # 高风险清单以外的动作不会被路由到这个节点（HIGH_RISK_PLATFORM_ACTIONS 只有这三个）
        logger.warning("request_confirmation 收到清单外的高风险动作", action=action)
        return {
            "reply_plan": {"mode": "template", "text": FALLBACK_LLM_UNAVAILABLE_REPLY},
            "tools_meta": [{"name": "platform_command", "status": "unknown_action"}],
        }

    pending_id = uuid.uuid4()
    confirm_text = _build_confirm_text(action, confirm_args)
    expires_at = now + timedelta(seconds=settings.pending_action_ttl_seconds)
    # 幂等键把 pending_id 拼进去：同一个待确认操作只对应一个幂等键，confirm_action 重试执行时
    # 用的是这同一个 key，不会因为重新生成 key 而让 mock-platform 判定成"新指令"重复执行
    idempotency_key = f"{tenant_id}:{conversation_id}:{action}:{pending_id}"

    session.add(
        PendingAction(
            id=pending_id,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=uuid.UUID(conversation_id),
            tool_name="platform_command",
            args=confirm_args,
            confirm_text=confirm_text,
            status=PendingActionStatus.pending,
            idempotency_key=idempotency_key,
            expires_at=expires_at,
        )
    )
    await session.commit()

    return {
        "reply_plan": {"mode": "template", "text": confirm_text},
        "pending_action_id": str(pending_id),
        "tools_meta": [{"name": "platform_command", "status": "pending_confirmation"}],
    }


async def confirm_action(state: GraphState, runtime) -> dict[str, Any]:
    session = runtime.context.session
    tenant_id = state["tenant_id"]
    conversation_id = state["conversation_id"]

    target = await _find_target_pending_action(session, tenant_id, conversation_id)
    if target is None:
        return {"reply_plan": {"mode": "template", "text": PLATFORM_ALREADY_PROCESSED_REPLY}}

    # 原子抢占：只有 status 还是 pending 且没过期才能抢到，抢到的同时把状态改成 executing，
    # 判断和占用是数据库里的同一步，不会有"两个请求都读到 pending 然后都去执行"的竞态
    acquire_stmt = (
        update(PendingAction)
        .where(
            PendingAction.id == target.id,
            PendingAction.status == PendingActionStatus.pending,
            PendingAction.expires_at > datetime.now(timezone.utc),
        )
        .values(status=PendingActionStatus.executing)
        .returning(PendingAction)
    )
    acquired = (await session.execute(acquire_stmt)).scalar_one_or_none()
    await session.commit()

    if acquired is None:
        refreshed = await session.get(PendingAction, target.id)
        if refreshed is not None and refreshed.status == PendingActionStatus.pending:
            # 状态还是 pending 说明没抢到的原因是过期，不是已经被处理过——顺手把状态标成
            # expired，避免这条记录一直挂着 pending 的假象
            await session.execute(
                update(PendingAction)
                .where(PendingAction.id == refreshed.id, PendingAction.status == PendingActionStatus.pending)
                .values(status=PendingActionStatus.expired)
            )
            await session.commit()
            return {"reply_plan": {"mode": "template", "text": PLATFORM_CONFIRM_TIMEOUT_REPLY}}
        return {"reply_plan": {"mode": "template", "text": PLATFORM_ALREADY_PROCESSED_REPLY}}

    action = acquired.args["action"]
    params = {k: v for k, v in acquired.args.items() if k != "action" and v is not None}

    try:
        response = await submit_command(
            tenant_id=tenant_id,
            user_id=acquired.user_id,
            action=action,
            params=params,
            idempotency_key=acquired.idempotency_key,
        )
        success = response.get("status") == "success"
        result_payload = response
    except PlatformUnavailable as exc:
        logger.warning("确认执行高风险平台指令失败", action=action, error=str(exc))
        success = False
        result_payload = {"error": str(exc)}

    new_status = PendingActionStatus.executed if success else PendingActionStatus.failed
    await session.execute(
        update(PendingAction).where(PendingAction.id == acquired.id).values(status=new_status, result=result_payload)
    )
    await _write_platform_audit(
        session,
        tenant_id=tenant_id,
        actor_user_id=acquired.user_id,
        actor_role=UserRole(state["role"]),
        action=action,
        result="success" if success else "failed",
        trace_id=state.get("trace_id"),
        conversation_id=conversation_id,
    )

    reply = _build_success_reply(action, acquired.args) if success else _build_failure_reply(action)
    return {
        "reply_plan": {"mode": "template", "text": reply},
        "pending_action_id": str(acquired.id),
        "tools_meta": [{"name": "platform_command", "status": "ok" if success else "upstream_error"}],
    }


async def cancel_action(state: GraphState, runtime) -> dict[str, Any]:
    session = runtime.context.session
    target = await _find_target_pending_action(session, state["tenant_id"], state["conversation_id"])
    if target is None:
        return {"reply_plan": {"mode": "template", "text": PLATFORM_ALREADY_PROCESSED_REPLY}}

    cancel_stmt = (
        update(PendingAction)
        .where(PendingAction.id == target.id, PendingAction.status == PendingActionStatus.pending)
        .values(status=PendingActionStatus.cancelled)
        .returning(PendingAction)
    )
    cancelled = (await session.execute(cancel_stmt)).scalar_one_or_none()
    await session.commit()

    if cancelled is None:
        return {"reply_plan": {"mode": "template", "text": PLATFORM_ALREADY_PROCESSED_REPLY}}

    state_text = _cancel_state_text(cancelled.args["action"], cancelled.args)
    return {
        "reply_plan": {"mode": "template", "text": f"好的，已取消。{state_text}。"},
        "pending_action_id": str(cancelled.id),
    }


async def confirm_ambiguous(state: GraphState, runtime) -> dict[str, Any]:
    """用户有待确认操作，却回复了"对/是的/好的"这类不含"确认"字样的短句（PHASE2.md 2.10 第 6 点）。

    判断"是不是这种句子"在 classify.py 里完成（短句 + 有未过期待确认 + 不含确认/取消关键词）；
    这里只负责查出具体待确认的是哪个动作，把提示语里的确认短语换成对应那句（"确认关闭"/
    "确认开通"/"确认提交"），而不是写死"确认关闭"——不然请假场景提示的话术就说错了。
    """
    session = runtime.context.session
    target = await _find_target_pending_action(session, state["tenant_id"], state["conversation_id"])
    now = datetime.now(timezone.utc)
    if target is not None and target.status == PendingActionStatus.pending and target.expires_at > now:
        phrase = _confirm_phrase(target.args.get("action", ""))
    else:
        phrase = "确认"
    return {"reply_plan": {"mode": "template", "text": f"为了避免误操作，这一步需要你回复“{phrase}”我才会处理。"}}
