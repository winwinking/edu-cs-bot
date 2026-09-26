"""阶段三冒烟测试（PHASE3.md 第 2 步：先放 E2E 场景 5，后续步骤继续往这个文件里加场景）。

跟 phase2_smoke.py 是同一套约定：真实起 docker compose、真实连 gateway/worker/数据库，
不是 pytest 用例；每个场景断言关键字段，打印 PASS/FAIL。
"""
import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import websockets
from sqlalchemy import func, select, update

from app.common.auth import create_access_token
from app.common.config import get_settings
from app.common.db import AsyncSessionLocal
from app.common.models import ConversationSummary, Reminder, ReminderStatus, Tenant, User

REPLY_TIMEOUT_SECONDS = 30
PUSH_TIMEOUT_SECONDS = 10
FAST_FORWARD_SECONDS = 3
GATEWAY_URL = "ws://gateway:8000/ws"

settings = get_settings()

_CONVERSATION_NAMESPACE = uuid.UUID("6f8f2c2e-1a4b-4e9a-9f1a-2c9a7e6d5b4a")


def _conversation_id(tenant: str, user: str, conv_label: str) -> str:
    return str(uuid.uuid5(_CONVERSATION_NAMESPACE, f"{tenant}:{user}:{conv_label}"))


async def _get_token(tenant: str, user: str) -> str:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.tenant_id == tenant, User.id == user))
        row = result.scalar_one_or_none()
        if row is None:
            raise SystemExit(f"用户不存在：tenant={tenant} user={user}，先跑 make seed")
        return create_access_token(user_id=row.id, tenant_id=row.tenant_id, role=row.role.value)


async def _set_mock_mode(service: str, **updates) -> None:
    async with httpx.AsyncClient(timeout=5) as client:
        resp = await client.post(f"http://mock-{service}:8000/admin/config", json=updates)
        resp.raise_for_status()


async def _send_and_wait(ws, conversation_id: str, content: str, message_id: str | None = None) -> dict:
    """给下面几个第 4/5/6/7 步场景用：复用同一条 WebSocket 连接连续发好几条消息（限流、熔断
    要在同一条连接上短时间内连发才有意义），跟 scenario_5/scenario_context_summary 里
    "每条消息各开一条新连接"的写法不是一回事，所以单独抽一个函数，不去改那两个已经跑通的场景。"""
    mid = message_id or str(uuid.uuid4())
    await ws.send(
        json.dumps({"type": "message", "message_id": mid, "conversation_id": conversation_id, "content": content})
    )
    ack = json.loads(await ws.recv())
    if ack.get("status") != "accepted":
        return {"ack": ack, "reply": "", "meta": {}}

    chunks: list[str] = []
    meta: dict = {}
    async with asyncio.timeout(REPLY_TIMEOUT_SECONDS):
        while True:
            msg = json.loads(await ws.recv())
            if msg["type"] == "reply_chunk":
                chunks.append(msg["delta"])
            elif msg["type"] == "reply_end":
                meta = msg.get("meta", {})
                break
    return {"ack": ack, "reply": "".join(chunks), "meta": meta}


async def _cancel_all_active_reminders(tenant: str, user: str) -> None:
    """提醒修改/取消场景要求"当前只有这一条生效中的提醒"（`_resolve_target_reminder` 找不到
    reminder_id 时，活跃提醒不止一条就会要求用户澄清，不会直接改/取消）；这个脚本可能反复跑，
    上一次跑剩下的提醒不清掉的话，这次会变成"需要澄清"而不是直接改/取消，场景就废了。"""
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(Reminder)
            .where(Reminder.tenant_id == tenant, Reminder.user_id == user, Reminder.status == ReminderStatus.active)
            .values(status=ReminderStatus.cancelled)
        )
        await session.commit()


async def _reset_all_mocks() -> None:
    async with httpx.AsyncClient(timeout=5) as client:
        for service in ("llm", "finance", "platform"):
            try:
                resp = await client.post(f"http://mock-{service}:8000/admin/reset")
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                print(f"[reset] {service} 跳过（{exc.__class__.__name__}）")


_PASS = "PASS"
_FAIL = "FAIL"


def _check(label: str, ok: bool, detail: str) -> tuple[str, bool]:
    result = _PASS if ok else _FAIL
    print(f"[{result}] {label}：{detail}")
    return label, ok


async def _fast_forward_latest_reminder(tenant: str, user: str) -> tuple[uuid.UUID, datetime]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Reminder.id)
            .where(Reminder.tenant_id == tenant, Reminder.user_id == user, Reminder.status == ReminderStatus.active)
            .order_by(Reminder.created_at.desc())
            .limit(1)
        )
        reminder_id = result.scalar_one_or_none()
        if reminder_id is None:
            raise SystemExit(f"没找到生效中的提醒：tenant={tenant} user={user}")

        stmt = (
            update(Reminder)
            .where(Reminder.id == reminder_id)
            .values(next_trigger_at=func.now() + timedelta(seconds=FAST_FORWARD_SECONDS))
            .returning(Reminder.next_trigger_at)
        )
        result = await session.execute(stmt)
        next_trigger_at = result.scalar_one()
        await session.commit()
    return reminder_id, next_trigger_at


async def scenario_5_reminder_push() -> tuple[str, bool]:
    """场景 5：用户创建"明天早上 9 点提醒我交作业" → reminders 表里有这一条 → 快进
    next_trigger_at → 在同一条 WebSocket 连接上收到提醒推送 → 推送延迟 < 5 秒。"""
    tenant, user, conv = "t_a", "u_a_1001", "s5"
    token = await _get_token(tenant, user)
    conversation_id = _conversation_id(tenant, user, conv)

    async with websockets.connect(f"{GATEWAY_URL}?token={token}") as ws:
        mid = str(uuid.uuid4())
        await ws.send(
            json.dumps(
                {
                    "type": "message",
                    "message_id": mid,
                    "conversation_id": conversation_id,
                    "content": "明天早上 9 点提醒我交作业",
                }
            )
        )
        ack = json.loads(await ws.recv())
        if ack.get("status") != "accepted":
            return _check("场景5 创建提醒并按时收到推送", False, f"ack={ack}")

        chunks: list[str] = []
        meta: dict = {}
        try:
            async with asyncio.timeout(REPLY_TIMEOUT_SECONDS):
                while True:
                    msg = json.loads(await ws.recv())
                    if msg["type"] == "reply_chunk":
                        chunks.append(msg["delta"])
                    elif msg["type"] == "reply_end":
                        meta = msg.get("meta", {})
                        break
        except TimeoutError:
            return _check("场景5 创建提醒并按时收到推送", False, "创建提醒的回复超时")

        reply_text = "".join(chunks)
        create_ok = "已设置提醒" in reply_text and "交作业" in reply_text

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Reminder)
                .where(Reminder.tenant_id == tenant, Reminder.user_id == user, Reminder.status == ReminderStatus.active)
                .order_by(Reminder.created_at.desc())
                .limit(1)
            )
            row = result.scalar_one_or_none()
        db_ok = row is not None and row.title == "交作业"
        if not (create_ok and db_ok):
            return _check(
                "场景5 创建提醒并按时收到推送", False, f"create_ok={create_ok} db_ok={db_ok} reply={reply_text[:40]!r}"
            )

        _, next_trigger_at = await _fast_forward_latest_reminder(tenant, user)

        pushed_at = None
        push_ok = False
        try:
            async with asyncio.timeout(PUSH_TIMEOUT_SECONDS):
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("type") == "reminder":
                        pushed_at = datetime.now(timezone.utc)
                        push_ok = "交作业" in msg.get("text", "")
                        break
        except TimeoutError:
            pass

        latency_seconds = (pushed_at - next_trigger_at).total_seconds() if pushed_at else None
        ok = push_ok and latency_seconds is not None and latency_seconds < 5
        detail = f"push_ok={push_ok} 延迟={latency_seconds}"
        return _check("场景5 创建提醒并按时收到推送", ok, detail)


async def scenario_context_summary() -> tuple[str, bool]:
    """第 3 步：同一会话连续发 25 条消息，超过阈值后应该生成历史摘要（conversation_summaries
    有记录），最后一条回复的 meta 里 context.has_summary 为 true。"""
    tenant, user, conv = "t_a", "u_a_1001", "s_context"
    token = await _get_token(tenant, user)
    conversation_id = _conversation_id(tenant, user, conv)

    last_meta: dict = {}
    async with websockets.connect(f"{GATEWAY_URL}?token={token}") as ws:
        for i in range(25):
            mid = str(uuid.uuid4())
            await ws.send(
                json.dumps(
                    {
                        "type": "message",
                        "message_id": mid,
                        "conversation_id": conversation_id,
                        "content": f"你好，这是第 {i + 1} 条消息",
                    }
                )
            )
            ack = json.loads(await ws.recv())
            if ack.get("status") != "accepted":
                return _check("场景(上下文) 25 条消息后生成历史摘要", False, f"第 {i + 1} 条 ack={ack}")

            try:
                async with asyncio.timeout(REPLY_TIMEOUT_SECONDS):
                    while True:
                        msg = json.loads(await ws.recv())
                        if msg["type"] == "reply_end":
                            last_meta = msg.get("meta", {})
                            break
            except TimeoutError:
                return _check("场景(上下文) 25 条消息后生成历史摘要", False, f"第 {i + 1} 条回复超时")

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ConversationSummary).where(ConversationSummary.conversation_id == uuid.UUID(conversation_id))
        )
        row = result.scalar_one_or_none()

    db_ok = row is not None and len(row.summary) > 0
    meta_ok = last_meta.get("context", {}).get("has_summary") is True
    ok = db_ok and meta_ok
    detail = f"db_ok={db_ok} meta={last_meta.get('context')}"
    return _check("场景(上下文) 25 条消息后生成历史摘要", ok, detail)


async def scenario_reminder_update_and_cancel() -> tuple[str, bool]:
    """第 7 步收尾场景：创建一条提醒 -> "提醒改成明天早上 10 点"（action=update，只有一条
    生效中的提醒时不用带 reminder_id 也能自动选中）-> 取消这条提醒。三步都检查回复文案，
    改完/取消完各查一次数据库确认真的落库了。"""
    tenant, user, conv = "t_a", "u_a_1001", "s_reminder_edit"
    await _cancel_all_active_reminders(tenant, user)
    token = await _get_token(tenant, user)
    conversation_id = _conversation_id(tenant, user, conv)

    async with websockets.connect(f"{GATEWAY_URL}?token={token}") as ws:
        r_create = await _send_and_wait(ws, conversation_id, "明天早上 9 点提醒我开会")
        create_ok = "已设置提醒" in r_create["reply"] and "开会" in r_create["reply"]

        r_update = await _send_and_wait(ws, conversation_id, "提醒改成明天早上 10 点")
        update_ok = "已把" in r_update["reply"] and "10:00" in r_update["reply"]

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Reminder)
                .where(Reminder.tenant_id == tenant, Reminder.user_id == user, Reminder.status == ReminderStatus.active)
                .order_by(Reminder.created_at.desc())
                .limit(1)
            )
            row = result.scalar_one_or_none()
        update_db_ok = row is not None and row.event_at.astimezone(timezone.utc).hour == 2  # 10:00 本地=UTC+8 -> 02:00 UTC

        r_cancel = await _send_and_wait(ws, conversation_id, "取消这条提醒")
        cancel_ok = "已取消" in r_cancel["reply"] and "开会" in r_cancel["reply"]

        async with AsyncSessionLocal() as session:
            result = await session.execute(select(Reminder.status).where(Reminder.id == row.id))
            status_after_cancel = result.scalar_one()
        cancel_db_ok = status_after_cancel == ReminderStatus.cancelled

    ok = create_ok and update_ok and update_db_ok and cancel_ok and cancel_db_ok
    detail = (
        f"create={r_create['reply'][:30]!r} update={r_update['reply'][:30]!r} "
        f"cancel={r_cancel['reply'][:30]!r} update_db_ok={update_db_ok} cancel_db_ok={cancel_db_ok}"
    )
    return _check("场景(提醒) 修改和取消", ok, detail)


async def scenario_rate_limit_degrade() -> tuple[str, bool]:
    """第 7 步收尾场景：10 秒内连发超过 RATE_LIMIT_USER_PER_10S 条消息，验证限流降级
    （PHASE3.md 第 4 步）。跟 scripts/rate_limit_burst.py 是同一个技巧：只读 ack、不等
    reply_chunk/reply_end——工作线程处理和 mock-llm 的延迟比一条条发消息慢得多，实测中
    所有 ack 都会在第一条回复开始流式返回之前就收完，不会读串。"""
    tenant, user = "t_a", "u_a_1002"  # 换一个别的场景不用的身份，不跟其它场景抢限流窗口
    limit = settings.rate_limit_user_per_10s
    count = limit + 5
    token = await _get_token(tenant, user)
    conversation_id = _conversation_id(tenant, user, "s_ratelimit")

    accepted = 0
    rate_limited = 0
    async with websockets.connect(f"{GATEWAY_URL}?token={token}") as ws:
        for i in range(count):
            await ws.send(
                json.dumps(
                    {
                        "type": "message",
                        "message_id": str(uuid.uuid4()),
                        "conversation_id": conversation_id,
                        "content": f"限流测试第 {i + 1} 条",
                    }
                )
            )
            ack = json.loads(await ws.recv())
            if ack.get("status") == "accepted":
                accepted += 1
            elif ack.get("status") == "rate_limited":
                rate_limited += 1

    ok = accepted == limit and rate_limited == count - limit
    detail = f"limit={limit} accepted={accepted} rate_limited={rate_limited}"
    return _check("场景(限流) 超过上限降级为 rate_limited", ok, detail)


async def scenario_circuit_breaker_degrade() -> tuple[str, bool]:
    """第 7 步收尾场景：mock-llm 切到 error500，连续失败触发熔断打开（meta.circuit_breaker
    含 "llm"）；不管这次运行之前熔断计数器上是不是已经带着别的测试留下的残留值，连发够
    CB_FAILURE_THRESHOLD+3 条一定能把熔断从任何起点推到打开——不依赖"第几条打开"这个具体
    数字（步骤 3 检查点 C 已经记录过按具体第几条断言不够稳的教训）。打开之后等
    CB_OPEN_SECONDS 恢复，确认不会把熔断状态遗留给后面的场景。"""
    tenant, user = "t_a", "u_a_1003"  # 坐席身份，跟其它场景分开，方便看日志时不跟别的会话混
    conversation_id = _conversation_id(tenant, user, "s_circuit")
    token = await _get_token(tenant, user)

    await _set_mock_mode("llm", mode="error500")
    saw_circuit_open = False
    try:
        async with websockets.connect(f"{GATEWAY_URL}?token={token}") as ws:
            for i in range(settings.cb_failure_threshold + 3):
                r = await _send_and_wait(ws, conversation_id, f"随便聊聊第 {i + 1} 句")
                if "llm" in r["meta"].get("circuit_breaker", []):
                    saw_circuit_open = True
                    break
    finally:
        await _set_mock_mode("llm", mode="normal")

    recovered = False
    if saw_circuit_open:
        await asyncio.sleep(settings.cb_open_seconds + 2)
        async with websockets.connect(f"{GATEWAY_URL}?token={token}") as ws:
            r = await _send_and_wait(ws, conversation_id, "熔断应该已经恢复了")
            recovered = r["meta"].get("circuit_breaker", []) == []

    ok = saw_circuit_open and recovered
    detail = f"saw_circuit_open={saw_circuit_open} recovered={recovered}（等了 {settings.cb_open_seconds + 2} 秒确认恢复)"
    return _check("场景(熔断) LLM 连续失败降级并自动恢复", ok, detail)


async def scenario_budget_degrade() -> tuple[str, bool]:
    """第 7 步收尾场景：机构 daily_token_budget 设成 0（不管今天用没用过都必定超限，比设成
    一个具体数字更稳定，不依赖这个机构今天已经用了多少），验证预算降级：意图识别跳过 LLM、
    关键词规则也判断不出意图时，回复的是"预算耗尽"专用话术（不是"系统这会儿有点忙"，见
    AGENT_LOG 步骤 6 审查修复第 1 条）。阶段四 4.1 起 t_b 的种子数据有真实预算（500000），
    不再是 NULL，所以测试前先读出原值，测试后恢复成这个原值，不能再无脑改回 NULL
    （PHASE4.md 4.1 明确要求）。"""
    tenant, user = "t_b", "u_b_1001"
    conversation_id = _conversation_id(tenant, user, "s_budget")
    token = await _get_token(tenant, user)

    async with AsyncSessionLocal() as session:
        original_budget = (
            await session.execute(select(Tenant.daily_token_budget).where(Tenant.id == tenant))
        ).scalar_one()
        await session.execute(update(Tenant).where(Tenant.id == tenant).values(daily_token_budget=0))
        await session.commit()

    try:
        async with websockets.connect(f"{GATEWAY_URL}?token={token}") as ws:
            r = await _send_and_wait(ws, conversation_id, "你好呀")
    finally:
        async with AsyncSessionLocal() as session:
            await session.execute(
                update(Tenant).where(Tenant.id == tenant).values(daily_token_budget=original_budget)
            )
            await session.commit()

    degrade_ok = r["meta"].get("budget_exceeded") is True and "这个问题我这边暂时处理不了" in r["reply"]

    async with websockets.connect(f"{GATEWAY_URL}?token={token}") as ws:
        r2 = await _send_and_wait(ws, conversation_id, "你好呀，预算应该恢复了")
    restored_ok = r2["meta"].get("budget_exceeded") is False

    ok = degrade_ok and restored_ok
    detail = f"degrade_reply={r['reply'][:40]!r} degrade_ok={degrade_ok} restored_ok={restored_ok}"
    return _check("场景(预算) 机构预算耗尽降级并恢复", ok, detail)


SCENARIOS = [
    scenario_5_reminder_push,
    scenario_context_summary,
    scenario_reminder_update_and_cancel,
    scenario_rate_limit_degrade,
    scenario_circuit_breaker_degrade,
    scenario_budget_degrade,
]


async def main() -> None:
    print("== 重置 mock 服务 ==")
    await _reset_all_mocks()

    results: list[tuple[str, bool]] = []
    for scenario in SCENARIOS:
        results.append(await scenario())

    print("== 重置 mock 服务 ==")
    await _reset_all_mocks()

    print("\n== 汇总 ==")
    for label, ok in results:
        print(f"{_PASS if ok else _FAIL}  {label}")

    failed = [label for label, ok in results if not ok]
    if failed:
        raise SystemExit(f"\n{len(failed)}/{len(results)} 个场景失败：{failed}")
    print(f"\n全部 {len(results)} 个场景 PASS")


if __name__ == "__main__":
    asyncio.run(main())
