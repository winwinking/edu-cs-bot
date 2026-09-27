"""日程提醒（PHASE3.md 第 2 步）。

创建、修改、取消、查看四个动作共用一个 manage_reminder 工具（app.common.tools.ManageReminderArgs）。

时间由 LLM 理解、代码检查（关键设计决定 6）：LLM 把"明天 9 点"这类说法转成本地时间字符串
放进 event_time，这里用 app.common.reminder_rules 把它换算成 UTC、检查是不是在将来、提前量
范围对不对——这些都是要连时区库才能判断的语义校验，跟 Pydantic 那层纯格式校验分开。

修改/取消时，LLM 应该从 <提醒列表> 块（本模块的 format_reminder_list_block，classify.py 在
组装 LLM 请求时调用）里选一个 id 抄回来；但这里不信任 LLM 给的 id 一定合法——重新查一遍数据库，
不属于当前 tenant/user 就按越权处理；id 缺失或选错也是合法结果，交给"0 条/1 条/多条"的分支
自己处理，不在参数校验那一层就直接拒绝。
"""
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select

from app.common.logging import get_logger
from app.common.models import Reminder, ReminderRepeat, ReminderStatus, Tenant
from app.common.reminder_rules import (
    ReminderRuleError,
    compute_creation_trigger,
    parse_local_datetime,
    resolve_timezone,
    validate_advance_minutes,
)
from app.worker.graph.state import GraphState
from app.worker.graph.style import (
    REMINDER_CREATE_FAILED_REPLY,
    REMINDER_FORBIDDEN_REPLY,
    REMINDER_NO_ACTIVE_REPLY,
)

logger = get_logger(__name__)

DEFAULT_ADVANCE_MINUTES = 30

_REPEAT_LABEL = {"daily": "，每天", "weekly": "，每周", "workdays": "，工作日"}


async def load_active_reminders(session, tenant_id: str, user_id: str) -> list[Reminder]:
    result = await session.execute(
        select(Reminder)
        .where(
            Reminder.tenant_id == tenant_id,
            Reminder.user_id == user_id,
            Reminder.status == ReminderStatus.active,
        )
        .order_by(Reminder.next_trigger_at)
    )
    return list(result.scalars().all())


def format_reminder_list_block(reminders: list[Reminder]) -> str:
    """给 LLM 看的 <提醒列表> 块：修改/取消时 LLM 应该从这里挑一个 id 抄回来（业务层面还会
    再核实一遍这个 id 真的属于当前 tenant/user，见本模块的 _resolve_target_reminder）。"""
    if not reminders:
        return "<提醒列表>\n（当前没有生效中的提醒）\n</提醒列表>"
    lines = []
    for r in reminders:
        tz = resolve_timezone(r.timezone)
        local = r.event_at.astimezone(tz)
        lines.append(f"id: {r.id} | 标题：{r.title} | 时间：{local:%Y-%m-%d %H:%M} | 重复：{r.repeat.value}")
    return "<提醒列表>\n" + "\n".join(lines) + "\n</提醒列表>"


def _format_list_line(r: Reminder, tz) -> str:
    local = r.event_at.astimezone(tz)
    return f"{local:%m 月 %d 日 %H:%M} {r.title}"


def _format_date_label_cn(dt_local: datetime, now_local: datetime) -> str:
    diff = (dt_local.date() - now_local.date()).days
    if diff == 0:
        return "今天"
    if diff == 1:
        return "明天"
    return f"{dt_local.month} 月 {dt_local.day} 日"


async def _load_tenant_timezone(session, tenant_id: str) -> str:
    result = await session.execute(select(Tenant.timezone).where(Tenant.id == tenant_id))
    return result.scalar_one()


def _rule_error_reply(exc: ReminderRuleError) -> dict[str, Any]:
    logger.warning("提醒的时间/规则校验没通过", reason=exc.reason, detail=exc.detail)
    return {
        "reply_plan": {"mode": "template", "text": REMINDER_CREATE_FAILED_REPLY},
        "tools_meta": [{"name": "manage_reminder", "status": exc.reason}],
    }


async def _handle_create(session, tenant_id: str, user_id: str, conversation_id: str, tenant_timezone: str, args: dict) -> dict[str, Any]:
    title = args["title"]
    repeat = args.get("repeat") or "none"
    advance_minutes = args.get("advance_minutes")
    if advance_minutes is None:
        advance_minutes = DEFAULT_ADVANCE_MINUTES

    try:
        validate_advance_minutes(advance_minutes)
        tz = resolve_timezone(tenant_timezone)
        event_at_local = parse_local_datetime(args["event_time"], tz)
        event_at_utc = event_at_local.astimezone(timezone.utc)
        now_utc = datetime.now(timezone.utc)
        next_trigger_at = compute_creation_trigger(event_at_utc, advance_minutes, now_utc)
    except ReminderRuleError as exc:
        return _rule_error_reply(exc)

    reminder_id = uuid.uuid4()
    session.add(
        Reminder(
            id=reminder_id,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=uuid.UUID(conversation_id),
            title=title,
            event_at=event_at_utc,
            timezone=tenant_timezone,
            repeat=ReminderRepeat(repeat),
            advance_minutes=advance_minutes,
            next_trigger_at=next_trigger_at,
            status=ReminderStatus.active,
        )
    )
    await session.commit()
    # PHASE4.md 4.6 人审发现同一批：只记动作、结果和 ID，不记标题/时间这类消息相关内容
    logger.info(
        "创建提醒",
        conversation_id=conversation_id,
        user_id=user_id,
        reminder_id=str(reminder_id),
        status="ok",
    )

    now_local = datetime.now(tz)
    date_label = _format_date_label_cn(event_at_local, now_local)
    reply = f"已设置提醒：{date_label} {event_at_local:%H:%M} {title}{_REPEAT_LABEL.get(repeat, '')}，提前 {advance_minutes} 分钟在 IM 通知你。"
    return {
        "reply_plan": {"mode": "template", "text": reply},
        "tools_meta": [{"name": "manage_reminder", "status": "ok"}],
    }


async def _handle_view(session, tenant_id: str, user_id: str, tenant_timezone: str) -> dict[str, Any]:
    reminders = await load_active_reminders(session, tenant_id, user_id)
    if not reminders:
        return {
            "reply_plan": {"mode": "template", "text": REMINDER_NO_ACTIVE_REPLY},
            "tools_meta": [{"name": "manage_reminder", "status": "ok"}],
        }
    tz = resolve_timezone(tenant_timezone)
    lines = [f"{_format_list_line(r, tz)}{_REPEAT_LABEL.get(r.repeat.value, '')}" for r in reminders]
    reply = "你目前生效中的提醒：" + "；".join(lines) + "。"
    return {
        "reply_plan": {"mode": "template", "text": reply},
        "tools_meta": [{"name": "manage_reminder", "status": "ok"}],
    }


async def _resolve_target_reminder(
    session, tenant_id: str, user_id: str, reminder_id_arg: Optional[str], active_reminders: list[Reminder], tenant_timezone: str
) -> tuple[Optional[Reminder], Optional[dict[str, Any]]]:
    """返回 (target, early_response)。early_response 非空时调用方直接把它当结果返回。"""
    if reminder_id_arg:
        try:
            candidate_uuid = uuid.UUID(reminder_id_arg)
        except ValueError:
            candidate_uuid = None
        if candidate_uuid is not None:
            candidate = await session.get(Reminder, candidate_uuid)
            if candidate is not None:
                if candidate.tenant_id != tenant_id or candidate.user_id != user_id:
                    # LLM 给的 id 不属于当前 tenant/user——不能因为 LLM 说了就信，按越权处理
                    logger.warning("提醒操作越权：id 不属于当前用户", reminder_id=reminder_id_arg)
                    return None, {
                        "reply_plan": {"mode": "template", "text": REMINDER_FORBIDDEN_REPLY},
                        "tools_meta": [{"name": "manage_reminder", "status": "forbidden"}],
                    }
                if candidate.status == ReminderStatus.active:
                    return candidate, None
                # 是自己的但已经不生效了（取消/完成过）：当成"没选中"处理，走下面的 0/1/多条逻辑

    if not active_reminders:
        return None, {
            "reply_plan": {"mode": "template", "text": REMINDER_NO_ACTIVE_REPLY},
            "tools_meta": [{"name": "manage_reminder", "status": "not_found"}],
        }
    if len(active_reminders) == 1:
        return active_reminders[0], None

    tz = resolve_timezone(tenant_timezone)
    lines = [_format_list_line(r, tz) for r in active_reminders]
    reply = "你有几条生效中的提醒：" + "；".join(lines) + "。要操作哪一条，麻烦说一下时间或名称。"
    return None, {
        "reply_plan": {"mode": "template", "text": reply},
        "tools_meta": [{"name": "manage_reminder", "status": "need_clarification"}],
    }


async def _handle_update_or_cancel(
    session, tenant_id: str, user_id: str, conversation_id: str, tenant_timezone: str, action: str, args: dict
) -> dict[str, Any]:
    active_reminders = await load_active_reminders(session, tenant_id, user_id)
    target, early = await _resolve_target_reminder(
        session, tenant_id, user_id, args.get("reminder_id"), active_reminders, tenant_timezone
    )
    if early is not None:
        return early
    assert target is not None

    if action == "cancel":
        target.status = ReminderStatus.cancelled
        await session.commit()
        logger.info(
            "取消提醒", conversation_id=conversation_id, user_id=user_id, reminder_id=str(target.id), status="ok"
        )
        return {
            "reply_plan": {"mode": "template", "text": f"已取消“{target.title}”的提醒。"},
            "tools_meta": [{"name": "manage_reminder", "status": "ok"}],
        }

    # action == "update"：只改 LLM 实际给出的字段，没提到的保持原样
    try:
        if args.get("event_time"):
            tz = resolve_timezone(tenant_timezone)
            event_at_local = parse_local_datetime(args["event_time"], tz)
            target.event_at = event_at_local.astimezone(timezone.utc)
            target.timezone = tenant_timezone
        if args.get("repeat"):
            target.repeat = ReminderRepeat(args["repeat"])
        if args.get("advance_minutes") is not None:
            validate_advance_minutes(args["advance_minutes"])
            target.advance_minutes = args["advance_minutes"]
        if args.get("title"):
            target.title = args["title"]

        now_utc = datetime.now(timezone.utc)
        target.next_trigger_at = compute_creation_trigger(target.event_at, target.advance_minutes, now_utc)
    except ReminderRuleError as exc:
        await session.rollback()
        return _rule_error_reply(exc)

    await session.commit()
    logger.info(
        "修改提醒", conversation_id=conversation_id, user_id=user_id, reminder_id=str(target.id), status="ok"
    )
    tz = resolve_timezone(tenant_timezone)
    local = target.event_at.astimezone(tz)
    reply = f"已把“{target.title}”的提醒改到 {local:%m 月 %d 日 %H:%M}，提前 {target.advance_minutes} 分钟通知你。"
    return {
        "reply_plan": {"mode": "template", "text": reply},
        "tools_meta": [{"name": "manage_reminder", "status": "ok"}],
    }


async def reminder(state: GraphState, runtime) -> dict[str, Any]:
    session = runtime.context.session
    tool_call = state.get("tool_call")
    if not tool_call or tool_call.get("name") != "manage_reminder":
        # LLM 调用本身失败（route_source=rule_fallback）或者输出没通过校验时会走到这里——
        # 提醒是要精确时间的操作，不能用"系统这会儿有点忙"这种暗示"稍后自动恢复"的通用话术，
        # 也不能猜时间，只能让用户重新说一次（PHASE3.md 第 2 步）
        return {
            "reply_plan": {"mode": "template", "text": REMINDER_CREATE_FAILED_REPLY},
            "tools_meta": [{"name": "manage_reminder", "status": "invalid_json"}],
        }

    args = tool_call["args"]
    action = args["action"]
    tenant_id = state["tenant_id"]
    user_id = state["user_id"]
    tenant_timezone = state.get("tenant_timezone") or await _load_tenant_timezone(session, tenant_id)

    if action == "create":
        return await _handle_create(session, tenant_id, user_id, state["conversation_id"], tenant_timezone, args)
    if action == "view":
        return await _handle_view(session, tenant_id, user_id, tenant_timezone)
    if action in ("update", "cancel"):
        return await _handle_update_or_cancel(
            session, tenant_id, user_id, state["conversation_id"], tenant_timezone, action, args
        )

    logger.warning("manage_reminder 收到未知 action", action=action)
    return {
        "reply_plan": {"mode": "template", "text": REMINDER_CREATE_FAILED_REPLY},
        "tools_meta": [{"name": "manage_reminder", "status": "unknown_action"}],
    }
