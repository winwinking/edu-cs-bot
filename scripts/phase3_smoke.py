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
from app.common.db import AsyncSessionLocal
from app.common.models import ConversationSummary, Reminder, ReminderStatus, User

REPLY_TIMEOUT_SECONDS = 30
PUSH_TIMEOUT_SECONDS = 10
FAST_FORWARD_SECONDS = 3
GATEWAY_URL = "ws://gateway:8000/ws"

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


SCENARIOS = [
    scenario_5_reminder_push,
    scenario_context_summary,
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
