"""阶段二收尾冒烟测试（PHASE2.md 2.12 第 1 点）。

把阶段二的 8 个 E2E 场景（REQUIREMENTS.md 6.3 的 1、2、3、4、6、7、8、10，场景 5 是提醒，
阶段三才做，不在这里）加上阶段一已经实现的场景 9（重复 message_id 只处理一次）串起来跑一遍，
每个场景检查关键字和 meta，打印 PASS/FAIL。跑之前和跑完都做一次全量 mock 重置，保证这个脚本
自己不依赖运行顺序留下的脏状态，也不会把故障模式（timeout/invalid_json）泄漏给下一次运行。

这个脚本只做断言 + 打印，不是 pytest 用例：阶段二的验证约定一直是"真实起 docker compose、
真实连 gateway/worker/数据库"（见每一步 PHASE2.md 的验证命令），跟 tests/unit 里那些不连
数据库的纯函数测试是两条不同的检验路径，收尾脚本延续前者。
"""
import asyncio
import json
import uuid

import httpx
import websockets
from sqlalchemy import select

from app.common.auth import create_access_token
from app.common.db import AsyncSessionLocal
from app.common.models import User

REPLY_TIMEOUT_SECONDS = 30
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


async def send(tenant: str, user: str, conv_label: str, content: str, message_id: str | None = None) -> dict:
    """发一条消息，等到 reply_end（或者 ack=duplicate 就提前返回）。返回
    {"ack": str, "reply": str, "meta": dict}，reply/meta 在 ack=duplicate 时是空值。"""
    token = await _get_token(tenant, user)
    conversation_id = _conversation_id(tenant, user, conv_label)
    mid = message_id or str(uuid.uuid4())

    async with websockets.connect(f"{GATEWAY_URL}?token={token}") as ws:
        await ws.send(
            json.dumps({"type": "message", "message_id": mid, "conversation_id": conversation_id, "content": content})
        )
        ack = json.loads(await ws.recv())
        if ack["status"] == "duplicate":
            return {"ack": "duplicate", "reply": "", "meta": {}, "message_id": mid}

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
                    elif msg["type"] == "error":
                        return {"ack": ack["status"], "reply": "", "meta": {"error": msg}, "message_id": mid}
        except TimeoutError:
            raise SystemExit(f"超过 {REPLY_TIMEOUT_SECONDS} 秒没收到 reply_end，场景无法继续")

        return {"ack": ack["status"], "reply": "".join(chunks), "meta": meta, "message_id": mid}


async def _set_mock_mode(service: str, **updates) -> None:
    async with httpx.AsyncClient(timeout=5) as client:
        resp = await client.post(f"http://mock-{service}:8000/admin/config", json=updates)
        resp.raise_for_status()


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


async def scenario_1_knowledge_qa() -> tuple[str, bool]:
    """场景 1：用户查询课程政策，机器人引用知识库回答。"""
    r = await send("t_a", "u_a_1001", "s1", "寒假班请假会退课时费吗？")
    ok = r["reply"].startswith("依据《课程服务协议》第 4.2 条") and len(r["meta"].get("citations", [])) > 0
    return _check("场景1 知识问答引用知识库", ok, r["reply"][:60])


async def scenario_2_finance_invoice() -> tuple[str, bool]:
    """场景 2：用户查询发票，机器人返回脱敏结果。"""
    r = await send("t_a", "u_a_1001", "s2", "我上个月的发票开了吗？")
    ok = "l***@example.com" in r["reply"] and "@example.com" in r["reply"] and "lin.xiaoyu" not in r["reply"]
    return _check("场景2 发票查询脱敏", ok, r["reply"][:60])


async def scenario_3_finance_forbidden() -> tuple[str, bool]:
    """场景 3：用户 A 查询用户 B 财务，返回越权拒绝（worker 层面的"403"）。"""
    r = await send("t_a", "u_a_1001", "s3", "帮我查一下 u_a_1004 的发票")
    tool_status = next((t["status"] for t in r["meta"].get("tools", []) if t["name"] == "query_finance"), None)
    ok = "不属于你" in r["reply"] and tool_status == "forbidden"
    return _check("场景3 越权查询被拒", ok, f"reply={r['reply'][:40]!r} tool_status={tool_status}")


async def scenario_4_disable_auto_renew() -> tuple[str, bool]:
    """场景 4：用户关闭自动续费，机器人二次确认后执行。"""
    r1 = await send("t_a", "u_a_1001", "s4", "帮我把自动续费关了")
    confirm_ok = "确认一下" in r1["reply"] and "确认关闭" in r1["reply"]
    r2 = await send("t_a", "u_a_1001", "s4", "确认关闭")
    executed_ok = "已关闭" in r2["reply"] and "春季数学班" in r2["reply"]
    ok = confirm_ok and executed_ok
    return _check("场景4 关闭自动续费二次确认后执行", ok, f"confirm={r1['reply'][:30]!r} final={r2['reply'][:30]!r}")


async def scenario_6_handoff_with_summary() -> tuple[str, bool]:
    """场景 6：用户说"转人工"，携带摘要转接。"""
    await send("t_a", "u_a_1001", "s6", "我上个月的发票开了吗？")
    r = await send("t_a", "u_a_1001", "s6", "转人工")
    ok = r["meta"].get("handoff_ticket_id") is not None and (
        "已为你转接人工客服" in r["reply"] or "人工客服现在不在线" in r["reply"]
    )
    return _check("场景6 转人工携带摘要", ok, f"ticket={r['meta'].get('handoff_ticket_id')}")


async def scenario_7_finance_timeout() -> tuple[str, bool]:
    """场景 7：财务系统超时，机器人不编造，回复"暂时查不到，已记录，稍后回复"。"""
    await _set_mock_mode("finance", mode="timeout")
    try:
        r = await send("t_a", "u_a_1001", "s7", "我上个月的发票开了吗？")
    finally:
        await _set_mock_mode("finance", mode="normal")
    ok = "财务系统暂时查不到你的信息" in r["reply"] and "¥" not in r["reply"] and "EDU-" not in r["reply"]
    return _check("场景7 财务超时不编造", ok, r["reply"][:60])


async def scenario_8_llm_invalid_json() -> tuple[str, bool]:
    """场景 8：LLM 返回非法 JSON，系统兜底，不执行工具。"""
    await _set_mock_mode("llm", mode="invalid_json")
    try:
        r = await send("t_a", "u_a_1001", "s8", "帮我把自动续费关了")
    finally:
        await _set_mock_mode("llm", mode="normal")
    tool_status = next((t["status"] for t in r["meta"].get("tools", []) if t["name"] == "platform_command"), None)
    ok = "我先不做任何处理" in r["reply"] and tool_status == "invalid_json"
    return _check("场景8 LLM 非法 JSON 兜底不执行工具", ok, f"reply={r['reply'][:40]!r} tool_status={tool_status}")


async def scenario_9_dedup() -> tuple[str, bool]:
    """场景 9（阶段一已实现）：重复 message_id 只处理一次。"""
    mid = str(uuid.uuid4())
    r1 = await send("t_a", "u_a_1001", "s9", "你好，在吗", message_id=mid)
    r2 = await send("t_a", "u_a_1001", "s9", "你好，在吗", message_id=mid)
    ok = r1["ack"] == "accepted" and r2["ack"] == "duplicate"
    return _check("场景9 重复 message_id 只处理一次", ok, f"ack1={r1['ack']} ack2={r2['ack']}")


async def scenario_10_knowledge_no_hit() -> tuple[str, bool]:
    """场景 10：知识库无命中，机器人不瞎编，建议转人工。"""
    r = await send("t_a", "u_a_1001", "s10", "你们的校车几点发车？")
    ok = "我暂时没有查到明确依据" in r["reply"] and len(r["meta"].get("citations", [])) == 0
    return _check("场景10 知识库无命中不瞎编", ok, r["reply"][:60])


SCENARIOS = [
    scenario_1_knowledge_qa,
    scenario_2_finance_invoice,
    scenario_3_finance_forbidden,
    scenario_4_disable_auto_renew,
    scenario_6_handoff_with_summary,
    scenario_7_finance_timeout,
    scenario_8_llm_invalid_json,
    scenario_9_dedup,
    scenario_10_knowledge_no_hit,
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
