"""财务查询节点（PHASE2.md 2.9）。

权限校验做两层：这里（worker）一层，mock-finance 自己一层（app/common/finance_client.py 里的
FinanceForbidden 就是第二层拒绝）。金额、订单号、状态全部来自 mock-finance 的返回值，不经过 LLM，
模板里的每个数字都能对应到接口返回的某个字段——这是"钱的事不能编"这条设计在代码里的体现。
"""
import uuid
from typing import Any, Optional

from sqlalchemy import select

from app.common.finance_client import FinanceForbidden, FinanceUnavailable, fetch_finance_data
from app.common.logging import get_logger
from app.common.masking import mask_bank_card, mask_email
from app.common.models import AuditLog, FollowupStatus, FollowupTask, GuardianLink, User, UserRole
from app.common.permissions import FinanceActor, can_access_finance
from app.worker.graph.state import GraphState
from app.worker.graph.style import FALLBACK_LLM_UNAVAILABLE_REPLY, FINANCE_FORBIDDEN_REPLY, FINANCE_UPSTREAM_ERROR_REPLY

logger = get_logger(__name__)


def _format_amount(amount: float) -> str:
    return f"¥{amount:,.0f}"


def _build_invoice_reply(invoices: list[dict]) -> str:
    if not invoices:
        return "我没有查到符合条件的订单和发票记录，换个时间范围问问？"
    inv = invoices[0]
    order_no = inv["order_no"]
    amount = _format_amount(inv["amount"])
    status = inv["status"]
    if status == "已开具" and inv.get("sent_at") and inv.get("email"):
        year, month, day = inv["sent_at"].split("-")
        return (
            f"我查到 {year}-{month} 有一笔订单 #{order_no}，金额 {amount}，发票状态：{status}，"
            f"电子发票已于 {int(month)} 月 {int(day)} 日发送到 {mask_email(inv['email'])}。需要我重发吗？"
        )
    return f"我查到有一笔订单 #{order_no}，金额 {amount}，发票状态：{status}。需要我帮你申请开具吗？"


def _build_order_reply(orders: list[dict]) -> str:
    if not orders:
        return "我没有查到符合条件的订单记录，换个时间范围问问？"
    o = orders[0]
    return f"我查到 {o['period']} 有一笔订单 #{o['order_no']}，{o['course_name']}，金额 {_format_amount(o['amount'])}。"


def _build_bill_reply(bills: list[dict]) -> str:
    if not bills:
        return "我没有查到符合条件的账单记录，换个时间范围问问？"
    b = bills[0]
    return (
        f"我查到 {b['period']} 有一笔账单 #{b['order_no']}，{b['course_name']}，"
        f"金额 {_format_amount(b['amount'])}，状态：{b['status']}。"
    )


def _build_refund_reply(refunds: list[dict]) -> str:
    if not refunds:
        return "我暂时没有查到你的退费记录。"
    r = refunds[0]
    if r.get("bank_card"):
        return (
            f"我查到你有一笔退费，状态：{r['status']}，金额 {_format_amount(r['amount'])}，"
            f"退回银行卡{mask_bank_card(r['bank_card'])}。"
        )
    return f"我查到你有一笔退费，状态：{r['status']}，金额 {_format_amount(r['amount'])}。"


def _build_balance_reply(balance: Optional[float]) -> str:
    if balance is None:
        return "我暂时没有查到你的账户余额。"
    return f"我查到你的账户余额为 ¥{balance:,.2f}。"


_REPLY_BUILDERS = {
    "invoices": lambda data: _build_invoice_reply(data.get("invoices", [])),
    "orders": lambda data: _build_order_reply(data.get("orders", [])),
    "bills": lambda data: _build_bill_reply(data.get("bills", [])),
    "refunds": lambda data: _build_refund_reply(data.get("refunds", [])),
    "balance": lambda data: _build_balance_reply(data.get("balance")),
}


async def _load_linked_student_ids(session, tenant_id: str, parent_user_id: str) -> frozenset[str]:
    result = await session.execute(
        select(GuardianLink.student_user_id).where(
            GuardianLink.tenant_id == tenant_id, GuardianLink.parent_user_id == parent_user_id
        )
    )
    return frozenset(row[0] for row in result.all())


async def _load_user(session, user_id: str) -> Optional[User]:
    result = await session.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


def _resolve_target_user_id(
    actor_user_id: str, actor_role: UserRole, target_user_id_arg: Optional[str], linked_student_ids: frozenset[str]
) -> str:
    if target_user_id_arg:
        return target_user_id_arg
    # 家长没指定要查谁、又只关联了一个学员时，默认查这个学员——家长自己通常没有课程订单
    if actor_role == UserRole.parent and len(linked_student_ids) == 1:
        return next(iter(linked_student_ids))
    return actor_user_id


async def _write_audit_log(
    session,
    *,
    tenant_id: str,
    actor_user_id: str,
    actor_role: UserRole,
    target_user: Optional[User],
    raw_target_user_id: str,
    kind: str,
    period: Optional[str],
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
            action="query_finance",
            # target_user_id 有外键约束，被查的 id 如果查无此人就不填这一列，避免插入失败；
            # 实际请求的是谁，无论存不存在都记在 detail 里，不影响审计追溯
            target_user_id=target_user.id if target_user else None,
            resource=f"finance:{kind}",
            result=result,
            trace_id=trace_id,
            conversation_id=uuid.UUID(conversation_id) if conversation_id else None,
            detail={"kind": kind, "period": period, "target_user_id": raw_target_user_id},
        )
    )
    await session.commit()


async def _write_followup_task(
    session, *, tenant_id: str, user_id: str, conversation_id: Optional[str], kind: str, period: Optional[str], target_user_id: str
) -> None:
    session.add(
        FollowupTask(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=uuid.UUID(conversation_id),
            kind="finance_query",
            query={"finance_kind": kind, "period": period, "target_user_id": target_user_id},
            status=FollowupStatus.open,
        )
    )
    await session.commit()


async def finance(state: GraphState, runtime) -> dict[str, Any]:
    session = runtime.context.session
    tool_call = state.get("tool_call")

    if not tool_call or tool_call.get("name") != "query_finance":
        # 没有 LLM 给的参数（比如走了 rule_fallback），不知道要查哪类数据、查谁，没法瞎猜，
        # 按"这会儿处理不了"处理
        return {
            "reply_plan": {"mode": "template", "text": FALLBACK_LLM_UNAVAILABLE_REPLY},
            "tools_meta": [{"name": "query_finance", "status": "invalid_json"}],
        }

    args = tool_call["args"]
    kind: str = args["kind"]
    period: Optional[str] = args.get("period")
    target_user_id_arg: Optional[str] = args.get("target_user_id")

    tenant_id = state["tenant_id"]
    actor_user_id = state["user_id"]
    actor_role = UserRole(state["role"])
    trace_id = state.get("trace_id")
    conversation_id = state.get("conversation_id")

    linked_student_ids = await _load_linked_student_ids(session, tenant_id, actor_user_id)
    target_user_id = _resolve_target_user_id(actor_user_id, actor_role, target_user_id_arg, linked_student_ids)
    target_user = await _load_user(session, target_user_id)

    actor = FinanceActor(
        tenant_id=tenant_id, user_id=actor_user_id, role=actor_role, linked_student_ids=linked_student_ids
    )
    allowed = target_user is not None and can_access_finance(actor, target_user.tenant_id, target_user_id)

    audit_kwargs = dict(
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        actor_role=actor_role,
        raw_target_user_id=target_user_id,
        kind=kind,
        period=period,
        trace_id=trace_id,
        conversation_id=conversation_id,
    )

    if not allowed:
        await _write_audit_log(session, target_user=target_user, result="forbidden", **audit_kwargs)
        return {
            "reply_plan": {"mode": "template", "text": FINANCE_FORBIDDEN_REPLY},
            "tools_meta": [{"name": "query_finance", "status": "forbidden"}],
        }

    try:
        data = await fetch_finance_data(
            kind,
            tenant_id=target_user.tenant_id,
            acting_user_id=actor_user_id,
            target_user_id=target_user_id,
            period=period,
        )
    except FinanceForbidden:
        # worker 这层判断通过了，但 mock-finance 自己又拒绝了——两层规则不一致的信号，
        # 阶段四要专门测"两层结果一致"，这里先如实按 mock-finance 的结果处理
        logger.warning("worker 权限校验通过，但 mock-finance 拒绝了，两层判断不一致", kind=kind)
        await _write_audit_log(session, target_user=target_user, result="forbidden", **audit_kwargs)
        return {
            "reply_plan": {"mode": "template", "text": FINANCE_FORBIDDEN_REPLY},
            "tools_meta": [{"name": "query_finance", "status": "forbidden"}],
        }
    except FinanceUnavailable as exc:
        logger.warning("财务系统查询失败，记一条跟进任务", kind=kind, error=str(exc))
        await _write_audit_log(session, target_user=target_user, result="upstream_error", **audit_kwargs)
        await _write_followup_task(
            session,
            tenant_id=tenant_id,
            user_id=actor_user_id,
            conversation_id=conversation_id,
            kind=kind,
            period=period,
            target_user_id=target_user_id,
        )
        return {
            "reply_plan": {"mode": "template", "text": FINANCE_UPSTREAM_ERROR_REPLY},
            "tools_meta": [{"name": "query_finance", "status": "upstream_error"}],
        }

    reply_text = _REPLY_BUILDERS[kind](data)
    await _write_audit_log(session, target_user=target_user, result="success", **audit_kwargs)
    return {
        "reply_plan": {"mode": "template", "text": reply_text},
        "tools_meta": [{"name": "query_finance", "status": "ok"}],
    }
