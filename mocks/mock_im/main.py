"""mock-im：阶段三第 7 步升级成演示控制台，3.8 步再加一版处理流程视图/坐席工作台。
这个网页扮演 IM 客户端直接连 gateway 的 WebSocket，支持身份切换、流式聊天、提醒推送、
分身份的 E2E 场景快捷发送、reply_end meta 透视（流程视图）、坐席工单+审计日志查看。

这个后端只做跟业务无关的辅助事：
- 发 HTML 页面；
- 按 tenant_id+user_id 现场签发开发用 token（`/api/token`，复用 app.common.auth.create_access_token，
  跟 scripts/chat.py 的签发方式一样，密钥来自环境变量，不写死在页面或代码里）——真实环境里 token
  由平台自己的登录系统签发，gateway 只管验签，这里签发只是演示时"扮演"平台登录系统；
- 给顶部状态条查 gateway 健康状态、机构今日 token 预算用量（只读，不影响预算判断本身）；
- 给右侧记忆区块查会话的历史摘要（只读，摘要入库前已经 mask_text() 脱敏，这里不用再脱一遍）；
- 给坐席工作台查本机构的转人工工单、审计日志（校验 token 角色必须是 agent、tenant_id 必须匹配）。
聊天本身的收发全部是页面里的 JS 直接连 gateway，这个后端不转发、不解析业务消息。
"""
import datetime as dt
import os
import uuid
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from app.common.auth import TokenError, TokenPayload, create_access_token, decode_access_token
from app.common.db import AsyncSessionLocal
from app.common.llm_usage import get_daily_budget
from app.common.masking import mask_text
from app.common.models import AuditLog, Conversation, ConversationSummary, HandoffTicket, Tenant, User
from app.common.redis import redis_client

app = FastAPI(title="mock-im")

_TEMPLATE_PATH = Path(__file__).parent / "templates" / "index.html"
# 这个 WebSocket 是浏览器直接连的，浏览器在宿主机上，所以要注入宿主机映射端口，不是容器内部端口
_GATEWAY_HOST_PORT = os.getenv("GATEWAY_HOST_PORT", "8000")
# 这个是 mock-im 后端自己查 gateway /health 用的，走 docker 网络内部地址，跟上面那个宿主机端口
# 是两回事——没有放进 app/common/config.py 的 Settings，因为这纯粹是 mock-im 自己的演示需要，
# 不是 gateway/worker/scheduler 会用到的配置，加进共用 Settings 没必要（阶段三 3.8 步的范围
# 限制是"只改 mocks/mock_im"，这里用跟 GATEWAY_HOST_PORT 一样的本地环境变量+默认值写法，
# 不碰 app/common）
_GATEWAY_INTERNAL_URL = os.getenv("GATEWAY_INTERNAL_URL", "http://gateway:8000")
_STATUS_HTTP_TIMEOUT_SECONDS = 2.0


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    html = _TEMPLATE_PATH.read_text(encoding="utf-8")
    return html.replace("__GATEWAY_HOST_PORT__", _GATEWAY_HOST_PORT)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/api/token")
async def issue_token(tenant_id: str, user_id: str) -> dict:
    """身份切换用：role 只有一个来源——数据库（CLAUDE.md 硬性规则），不能让前端自己传/猜，
    也不能让前端自己拼 JWT（密钥不能出现在页面或浏览器可见的代码里）。"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.tenant_id == tenant_id, User.id == user_id))
        row = result.scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail=f"用户不存在：tenant={tenant_id} user={user_id}，先跑 make seed")
        token = create_access_token(user_id=row.id, tenant_id=row.tenant_id, role=row.role.value)
        return {"token": token, "name": row.name, "role": row.role.value}


def _require_same_tenant(token: Optional[str], tenant_id: str) -> TokenPayload:
    """任何角色都能调，但 token 必须有效、token 里的机构必须跟请求的机构一致——不区分角色，
    只挡"拿别的机构的 token 来查这个机构的数据"（阶段三 3.8 补充决定 C）。"""
    if not token:
        raise HTTPException(status_code=403, detail="缺少 token")
    try:
        payload = decode_access_token(token)
    except TokenError:
        raise HTTPException(status_code=403, detail="token 无效")
    if payload.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="token 所属机构和请求的机构不一致")
    return payload


def _require_agent(token: Optional[str], tenant_id: str) -> TokenPayload:
    """坐席专用接口的权限校验：token 必须能解出来、角色必须是 agent、token 里的机构必须跟
    请求的机构一致——三条缺一个都当越权处理，返回 403，不区分"没传 token"和"角色不对"这两种
    情况给前端（都是"你没资格看这个"），但日志/异常里保留具体原因方便排查。
    """
    if not token:
        raise HTTPException(status_code=403, detail="缺少 token，仅坐席可访问")
    try:
        payload = decode_access_token(token)
    except TokenError:
        raise HTTPException(status_code=403, detail="token 无效，仅坐席可访问")
    if payload.role != "agent" or payload.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="仅坐席可访问，且只能查看本机构数据")
    return payload


def _budget_used_key(tenant_id: str, tenant_timezone: str) -> str:
    """必须跟 app/common/llm_usage.py 的 _budget_key() 保持完全一致的格式，这里是只读地
    展示当前用量，不是另一套判断逻辑——两处格式不一致的话，这里读到的用量会跟真实生效的预算
    判断对不上，比不实现这个展示还糟糕。"""
    today = dt.datetime.now(ZoneInfo(tenant_timezone)).strftime("%Y-%m-%d")
    return f"llm:budget:{tenant_id}:{today}"


@app.get("/api/status")
async def status(tenant_id: str = Query(...), token: Optional[str] = Query(None)) -> dict:
    """顶部状态条用（阶段三 3.8 第 7 点）：gateway 健康状态 + 当前机构今日 token 用量/预算。
    两者都是"看一眼"的展示数据，查询失败就报 unknown/None，不能因为这个接口挂了连累聊天功能。
    token 用量属于机构级别的运营数据，不是任何用户都该能查任意机构的（阶段三 3.8 补充决定 C），
    所以也要校验 token 所属机构跟请求的 tenant_id 一致，不区分角色（学生也能看自己机构的用量）。
    """
    _require_same_tenant(token, tenant_id)
    gateway_status = "unknown"
    try:
        async with httpx.AsyncClient(timeout=_STATUS_HTTP_TIMEOUT_SECONDS) as client:
            resp = await client.get(f"{_GATEWAY_INTERNAL_URL}/health")
            resp.raise_for_status()
            gateway_status = resp.json().get("status", "unknown")
    except Exception:
        gateway_status = "unknown"

    async with AsyncSessionLocal() as session:
        tenant = await session.get(Tenant, tenant_id)
        if tenant is None:
            raise HTTPException(status_code=404, detail=f"机构不存在：{tenant_id}")
        tenant_timezone = tenant.timezone
        budget = await get_daily_budget(session, tenant_id)

    used: Optional[int] = None
    try:
        raw = await redis_client.get(_budget_used_key(tenant_id, tenant_timezone))
        used = int(raw) if raw is not None else 0
    except Exception:
        used = None

    return {"gateway": gateway_status, "budget": {"used": used, "limit": budget}}


@app.get("/api/conversation/context")
async def conversation_context(
    tenant_id: str = Query(...), conversation_id: str = Query(...), token: Optional[str] = Query(None)
) -> dict:
    """右侧记忆区块用（阶段三 3.8 第 3 点）：会话的历史摘要覆盖到哪、摘要内容是什么。
    这是"当前最新状态"，不是点开的那条历史回复发生时的快照——reply_end meta 只记了
    "本轮带没带摘要"，没有存摘要内容本身，历史快照做不到，页面上要如实标注这一点。

    权限（阶段三 3.8 补充决定 C）：只能看 token 本人、本机构的会话——摘要内容虽然已经脱敏，
    但仍然是这个用户的对话内容，不能靠"猜一个 conversation_id"就看到别人的，校验方式是拿
    conversation_id 查 conversations 表，看 tenant_id/user_id 是不是都跟 token 对得上。
    """
    payload = _require_same_tenant(token, tenant_id)
    try:
        conversation_uuid = uuid.UUID(conversation_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="conversation_id 不是合法的 UUID")
    async with AsyncSessionLocal() as session:
        conversation = await session.get(Conversation, conversation_uuid)
        if conversation is None or conversation.tenant_id != tenant_id or conversation.user_id != payload.sub:
            raise HTTPException(status_code=403, detail="只能查看自己的会话")

        result = await session.execute(
            select(ConversationSummary).where(
                ConversationSummary.tenant_id == tenant_id,
                ConversationSummary.conversation_id == conversation_id,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            return {"summary": None, "covered_until": None}
        return {"summary": row.summary, "covered_until": row.covered_until.isoformat()}


def _serialize_handoff_ticket(row: HandoffTicket) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "user_id": row.user_id,
        "trigger": row.trigger.value,
        "intent": row.intent,
        "summary": row.summary,
        "attempted_actions": row.attempted_actions,
        "risk_flags": row.risk_flags,
        "status": row.status.value,
        "created_at": row.created_at.isoformat(),
    }


@app.get("/api/handoff_tickets")
async def handoff_tickets(tenant_id: str = Query(...), token: Optional[str] = Query(None)) -> dict:
    """坐席工作台用（阶段三 3.8 第 6 点）：本机构的转接单列表，非坐席角色/跨机构一律 403。"""
    _require_agent(token, tenant_id)
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(HandoffTicket)
            .where(HandoffTicket.tenant_id == tenant_id)
            .order_by(HandoffTicket.created_at.desc())
            .limit(50)
        )
        rows = result.scalars().all()
    return {"tickets": [_serialize_handoff_ticket(row) for row in rows]}


_AUDIT_RESULT_LABEL = {"success": "允许", "ok": "允许", "forbidden": "拒绝", "upstream_error": "失败", "failed": "失败"}


def _serialize_audit_log(row: AuditLog) -> dict[str, Any]:
    # detail 里存的字段（kind/period/target_user_id/action）本来就不含姓名、卡号这类原文，
    # 但显示内容必须脱敏是硬性要求（阶段三 3.8 第 10 点），这里统一过一遍 mask_text 兜底，
    # 不假设"这张表现在存的内容就一定不敏感"
    detail_text = mask_text(str(row.detail))
    return {
        "id": str(row.id),
        "created_at": row.created_at.isoformat(),
        "actor_user_id": row.actor_user_id,
        "actor_role": row.actor_role.value,
        "action": row.action,
        "target_user_id": row.target_user_id,
        "resource": row.resource,
        "result": row.result,
        "result_label": _AUDIT_RESULT_LABEL.get(row.result, row.result),
        "detail": detail_text,
    }


@app.get("/api/audit_logs")
async def audit_logs(tenant_id: str = Query(...), token: Optional[str] = Query(None)) -> dict:
    """坐席工作台"审计日志"栏用（阶段三 3.8 第 10 点）：本机构的财务查询+指令执行审计记录，
    非坐席角色/跨机构一律 403。"""
    _require_agent(token, tenant_id)
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(AuditLog)
            .where(AuditLog.tenant_id == tenant_id)
            .order_by(AuditLog.created_at.desc())
            .limit(50)
        )
        rows = result.scalars().all()
    return {"logs": [_serialize_audit_log(row) for row in rows]}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
