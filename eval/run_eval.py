"""LLM 质量评测脚本（docs/PHASE5.md 5.4）。

前提：已经 `make up`。用真实 WebSocket 链路把 `eval/cases.jsonl` 的题目挨个发一遍（多轮题
在同一个会话里顺序发），收集回复原文和 `meta`，按需要查数据库/mock-platform（见
`eval/db_checks.py`），用纯规则打分（见 `eval/scoring.py`），每题明细写到 `eval/output/`
（不进 git），汇总打印到终端并写成 `docs/EVAL_REPORT.md`。

用法：
    docker compose run --rm tools python eval/run_eval.py
"""
import asyncio
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
import websockets
from sqlalchemy import select, update

from app.common.auth import create_access_token
from app.common.db import AsyncSessionLocal
from app.common.llm_usage import get_daily_budget, is_budget_exceeded
from app.common.models import LlmUsage, PendingAction, PendingActionStatus, Reminder, ReminderStatus, Tenant, User

from eval import db_checks, scoring

GATEWAY_URL = "ws://gateway:8000/ws"
REPLY_TIMEOUT_SECONDS = 15  # docs/PHASE5.md 5.4 原文要求

EVAL_DIR = Path(__file__).parent
CASES_PATH = EVAL_DIR / "cases.jsonl"
OUTPUT_DIR = EVAL_DIR / "output"
REPORT_PATH = EVAL_DIR.parent / "docs" / "EVAL_REPORT.md"

SERVICE_HEALTH_URLS = {
    "gateway": "http://gateway:8000/ready",
    "worker": "http://worker:8001/health",
    "scheduler": "http://scheduler:8002/health",
    "mock-llm": "http://mock-llm:8000/health",
    "mock-finance": "http://mock-finance:8000/health",
    "mock-platform": "http://mock-platform:8000/health",
    "mock-knowledge": "http://mock-knowledge:8000/health",
}

TENANTS_TO_CHECK = ["t_a", "t_b"]

# 事实准确率的范围：有 must_contain/must_not_contain 字段的几类（SCORING.md 指标 1）
_FACT_CATEGORIES = {"知识问答命中", "财务合法查询", "财务越权", "平台指令", "提醒"}
# 少 AI 味评分里"具体信息"判定条件适用的业务意图类（拒答/拒绝场景不检查）
_SPECIFIC_INFO_CATEGORIES = {"知识问答命中", "财务合法查询", "平台指令", "提醒"}


class PreflightError(RuntimeError):
    pass


async def _check_services_healthy(client: httpx.AsyncClient) -> None:
    problems = []
    for name, url in SERVICE_HEALTH_URLS.items():
        try:
            resp = await client.get(url, timeout=5)
            if resp.status_code != 200:
                problems.append(f"{name}: HTTP {resp.status_code} {resp.text[:200]}")
        except httpx.HTTPError as exc:
            problems.append(f"{name}: {exc.__class__.__name__}: {exc}")
    if problems:
        raise PreflightError("以下服务不是 healthy 状态，先 make up / make ps 查看：\n" + "\n".join(problems))


async def _check_mocks_reset(client: httpx.AsyncClient) -> None:
    problems = []
    configs = {}
    for service in ("llm", "finance", "platform"):
        resp = await client.get(f"http://mock-{service}:8000/admin/config", timeout=5)
        resp.raise_for_status()
        config = resp.json()
        configs[service] = config
        if config.get("mode") != "normal":
            problems.append(f"mock-{service}.mode={config.get('mode')!r}，不是 normal")
    if configs.get("platform", {}).get("agents_online") is not True:
        problems.append(f"mock-platform.agents_online={configs.get('platform', {}).get('agents_online')!r}，不是 True")
    if problems:
        raise PreflightError(
            "mock 服务没有全部 reset（先跑 python scripts/mockctl.py all reset）：\n" + "\n".join(problems)
        )


async def _check_budgets() -> None:
    problems = []
    async with AsyncSessionLocal() as session:
        for tenant_id in TENANTS_TO_CHECK:
            tenant_tz = (await session.execute(select(Tenant.timezone).where(Tenant.id == tenant_id))).scalar_one()
            budget = await get_daily_budget(session, tenant_id)
            if budget is not None and await is_budget_exceeded(tenant_id, tenant_tz, budget):
                problems.append(f"{tenant_id} 今日 LLM token 预算已用完（budget={budget}）")
    if problems:
        raise PreflightError("机构预算已耗尽，跑评测会被预算降级掩盖真实结果：\n" + "\n".join(problems))


async def preflight() -> None:
    async with httpx.AsyncClient() as client:
        await _check_services_healthy(client)
        await _check_mocks_reset(client)
    await _check_budgets()
    print("[preflight] 容器 healthy、mock 全部 reset、两家机构预算未耗尽，开始评测。")


def load_cases() -> list[dict]:
    cases = []
    with open(CASES_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                cases.append(json.loads(line))
    return cases


def _tenant_user_pairs(cases: list[dict]) -> set[tuple[str, str]]:
    return {(c["tenant_id"], c["user_id"]) for c in cases}


async def reset_platform_subscriptions(client: httpx.AsyncClient) -> None:
    """mock-platform 已有的 /admin/reset 会把 _SUBSCRIPTIONS（自动续费状态）、幂等表、
    已执行指令列表都恢复成默认值（见 mocks/mock_platform/main.py reset_config()），直接复用，
    不用另外加接口。"""
    resp = await client.post("http://mock-platform:8000/admin/reset", timeout=5)
    resp.raise_for_status()


async def reset_eval_state(session, client: httpx.AsyncClient, cases: list[dict]) -> None:
    """开跑前把评测账号的可变业务状态恢复到固定起点（eval/PENDING_DECISIONS.md 问题 1/3）：
    - 生效中的提醒：取消（改状态），不删除——`rem02`/`rem03` 靠"当前只有一条生效中的提醒"
      自动定位目标，多次运行遗留的旧提醒会让系统落到"需要澄清"分支，答完全不同的话。
    - 未完成的待确认操作：标记过期，不删除——虽然 `_find_target_pending_action` 按
      conversation_id 过滤、理论上不会跨题目串号，但同一批调试遗留的大量 pending 记录会让
      数据库状态失真，趁这次一起清干净。
    - mock-platform 的自动续费状态：调用它自己的 /admin/reset 恢复默认订阅（`cmd01` 会真的
      关闭"春季数学班"的自动续费，不重置的话后面同一批次或者下一次整体重跑都会从"已关闭"
      这个不是默认值的状态开始）。

    另外排查过 dissatisfied_count（按 conversation 记，每题都是全新 conversation_id，不会
    跨题目累积）、Redis 限流计数（10 秒自然过期，加了题目间隔后不会触发，不需要主动清）、
    token 预算计数（应该真实累积，preflight 已经检查过没有耗尽）、audit_logs/handoff_tickets
    （本来就应该越攒越多，db_checks 已经按 conversation_id 过滤，不受历史记录干扰）——
    这几类不需要在这里重置。
    """
    pairs = _tenant_user_pairs(cases)
    for tenant_id, user_id in pairs:
        await session.execute(
            update(Reminder)
            .where(Reminder.tenant_id == tenant_id, Reminder.user_id == user_id, Reminder.status == ReminderStatus.active)
            .values(status=ReminderStatus.cancelled)
        )
        await session.execute(
            update(PendingAction)
            .where(
                PendingAction.tenant_id == tenant_id,
                PendingAction.user_id == user_id,
                PendingAction.status == PendingActionStatus.pending,
            )
            .values(status=PendingActionStatus.expired)
        )
    await session.commit()
    await reset_platform_subscriptions(client)
    print(f"[reset] 已取消 {len(pairs)} 个评测账号的生效中提醒、过期未完成的待确认操作、恢复 mock-platform 默认订阅状态。")


async def _get_token(session, tenant: str, user: str) -> str:
    result = await session.execute(select(User).where(User.tenant_id == tenant, User.id == user))
    row = result.scalar_one_or_none()
    if row is None:
        raise SystemExit(f"用户不存在：tenant={tenant} user={user}，先跑 make seed")
    return create_access_token(user_id=row.id, tenant_id=row.tenant_id, role=row.role.value)


async def _apply_setup(client: httpx.AsyncClient, setup: dict) -> None:
    for service, updates in setup.items():
        resp = await client.post(f"http://mock-{service}:8000/admin/config", json=updates, timeout=5)
        resp.raise_for_status()


async def _reset_setup(client: httpx.AsyncClient, setup: dict) -> None:
    for service in setup:
        try:
            resp = await client.post(f"http://mock-{service}:8000/admin/reset", timeout=5)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            print(f"[warn] 恢复 mock-{service} 配置失败：{exc}", file=sys.stderr)


async def _send_turn(ws, conversation_id: str, content: str) -> dict:
    message_id = str(uuid.uuid4())
    await ws.send(
        json.dumps({"type": "message", "message_id": message_id, "conversation_id": conversation_id, "content": content})
    )
    ack = json.loads(await ws.recv())
    turn: dict = {"content": content, "message_id": message_id, "ack": ack, "reply": "", "meta": {}, "error": None}
    if ack.get("status") != "accepted":
        return turn

    chunks: list[str] = []
    try:
        async with asyncio.timeout(REPLY_TIMEOUT_SECONDS):
            while True:
                msg = json.loads(await ws.recv())
                if msg["type"] == "reply_chunk":
                    chunks.append(msg["delta"])
                elif msg["type"] == "reply_end":
                    turn["meta"] = msg.get("meta", {})
                    break
                elif msg["type"] == "error":
                    turn["error"] = {"code": msg.get("code"), "detail": msg.get("detail")}
                    break
    except TimeoutError:
        turn["error"] = {"code": "eval_timeout", "detail": f"超过 {REPLY_TIMEOUT_SECONDS} 秒没收到 reply_end"}
    turn["reply"] = "".join(chunks)
    return turn


async def run_case(case: dict) -> dict:
    tenant_id = case["tenant_id"]
    user_id = case["user_id"]
    conversation_id = str(uuid.uuid4())
    setup = case.get("setup")

    async with AsyncSessionLocal() as session:
        token = await _get_token(session, tenant_id, user_id)

    async with httpx.AsyncClient() as client:
        if setup:
            await _apply_setup(client, setup)
        try:
            url = f"{GATEWAY_URL}?token={token}"
            turns = []
            async with websockets.connect(url) as ws:
                for content in case["turns"]:
                    turns.append(await _send_turn(ws, conversation_id, content))
        finally:
            if setup:
                await _reset_setup(client, setup)

    final = turns[-1]
    return {
        "case_id": case["id"],
        "conversation_id": conversation_id,
        "turns": turns,
        "final_reply": final["reply"],
        "final_meta": final["meta"],
        "final_error": final["error"],
    }


def _turn_reply(result: dict, index: int) -> str:
    if index < len(result["turns"]):
        return result["turns"][index]["reply"]
    return ""


def evaluate_text_checks(case: dict, result: dict) -> dict:
    expect = case["expect"]
    reply = result["final_reply"]
    meta = result["final_meta"]
    checks: dict = {}

    if "intent" in expect:
        checks["intent"] = {"ok": meta.get("intent") == expect["intent"], "got": meta.get("intent"), "want": expect["intent"]}

    if "intent_turn1" in expect:
        # 两轮高风险指令题：第一轮（确认前）和第二轮（确认那句话）的 intent 本来就不一样
        # （分别是 high_risk / confirm_action），只检查第一轮，第二轮是否真的执行交给 db_check
        # 判断（见 eval/PENDING_DECISIONS.md 问题 2）
        turn1_meta = result["turns"][0]["meta"] if result["turns"] else {}
        checks["intent_turn1"] = {
            "ok": turn1_meta.get("intent") == expect["intent_turn1"],
            "got": turn1_meta.get("intent"),
            "want": expect["intent_turn1"],
        }

    if "must_contain" in expect:
        ok, missing = scoring.check_must_contain(reply, expect["must_contain"])
        checks["must_contain"] = {"ok": ok, "missing": missing}

    if "must_not_contain" in expect:
        ok, leaked = scoring.check_must_not_contain(reply, expect["must_not_contain"])
        checks["must_not_contain"] = {"ok": ok, "leaked": leaked}

    if "must_contain_turn1" in expect:
        ok, missing = scoring.check_must_contain(_turn_reply(result, 0), expect["must_contain_turn1"])
        checks["must_contain_turn1"] = {"ok": ok, "missing": missing}

    if "must_contain_turn2" in expect:
        ok, missing = scoring.check_must_contain(_turn_reply(result, 1), expect["must_contain_turn2"])
        checks["must_contain_turn2"] = {"ok": ok, "missing": missing}

    if "citations" in expect:
        ok, detail = scoring.check_citations(reply, meta.get("citations", []), expect["citations"])
        checks["citations"] = {"ok": ok, "detail": detail}

    if expect.get("refuse"):
        checks["refuse"] = {"ok": scoring.is_finance_forbidden_reply(reply) or scoring.is_sensitive_reply(reply)}

    if "handoff" in expect:
        actual = bool(meta.get("handoff_ticket_id"))
        checks["handoff"] = {"ok": actual == expect["handoff"], "got": actual, "want": expect["handoff"]}
    else:
        actual = bool(meta.get("handoff_ticket_id"))
        checks["handoff"] = {"ok": actual is False, "got": actual, "want": False}

    return checks


def evaluate_ai_flavor(case: dict, result: dict) -> dict:
    expect = case["expect"]
    category = case["category"]
    reply = result["final_reply"]
    check_specific_info = category in _SPECIFIC_INFO_CATEGORIES and not expect.get("refuse") and not expect.get("uncertain")
    check_uncertain = bool(expect.get("uncertain"))
    score, deductions = scoring.score_ai_flavor(reply, check_specific_info=check_specific_info, check_uncertain=check_uncertain)
    return {"score": score, "deductions": deductions}


async def evaluate_case(case: dict, session, client: httpx.AsyncClient) -> dict:
    if case["category"] == "平台指令":
        # cmd01/cmd02/cmd05 都会真的改动 mock-platform 的自动续费订阅状态，题目之间不隔离
        # 的话后面的题目会从"已经被前一题改过"的状态起跑（见 eval/PENDING_DECISIONS.md
        # 问题 1）；每道平台指令题开始前都恢复一次默认订阅状态，不只是整个运行开始前恢复一次
        await reset_platform_subscriptions(client)
    if case["category"] == "提醒":
        # 排查 Jo 要求的"还有没有其他跨题目共享状态"时发现的同一类问题：rem01 创建的提醒
        # 一直是 active，rem02/rem03 靠"当前只有一条生效中的提醒"自动定位目标
        # （_resolve_target_reminder，app/worker/graph/reminder.py），题目之间不隔离的话
        # rem02 起跑时已经有 rem01 留下的一条在生效，会落到"需要澄清"分支；跟"平台指令"是
        # 同一种问题（题目之间共享了同一个用户名下的可变状态），套用同样的做法：每道提醒题
        # 开始前先取消这个用户当前生效中的提醒（不影响其它题目，因为 reminders 只按
        # tenant_id/user_id 分组，不区分是哪道题创建的）
        await session.execute(
            update(Reminder)
            .where(
                Reminder.tenant_id == case["tenant_id"],
                Reminder.user_id == case["user_id"],
                Reminder.status == ReminderStatus.active,
            )
            .values(status=ReminderStatus.cancelled)
        )
        await session.commit()
    result = await run_case(case)
    text_checks = evaluate_text_checks(case, result)
    ai_flavor = evaluate_ai_flavor(case, result)
    db_ok, db_detail = await db_checks.verify_db_check(case, result, session, client)

    forbidden_triplet = None
    if case["category"] == "财务越权":
        audit_forbidden = db_ok is True
        passed, detail = scoring.evaluate_forbidden_triplet(
            result["final_reply"], case["expect"].get("must_not_contain", []), audit_forbidden
        )
        forbidden_triplet = {"ok": passed, "detail": detail}

    all_text_ok = all(v["ok"] for v in text_checks.values())
    overall_ok = all_text_ok and (db_ok is not False)
    if forbidden_triplet is not None:
        overall_ok = overall_ok and forbidden_triplet["ok"]

    return {
        "id": case["id"],
        "category": case["category"],
        "turns": result["turns"],
        "final_reply": result["final_reply"],
        "final_meta": result["final_meta"],
        "final_error": result["final_error"],
        "text_checks": text_checks,
        "db_check": {"applicable": db_ok is not None, "ok": db_ok, "detail": db_detail},
        "forbidden_triplet": forbidden_triplet,
        "ai_flavor": ai_flavor,
        "overall_ok": overall_ok,
    }


def _agg_ratio(numerator: int, denominator: int) -> str:
    if denominator == 0:
        return "0/0（无适用题目）"
    pct = numerator / denominator * 100
    return f"{numerator}/{denominator}（{pct:.1f}%）"


def aggregate(records: list[dict]) -> dict:
    by_id = {r["id"]: r for r in records}

    # 指标 1：事实准确率
    fact_cases = [r for r in records if r["category"] in _FACT_CATEGORIES]
    fact_pass = [r for r in fact_cases if all(v["ok"] for k, v in r["text_checks"].items() if k in ("must_contain", "must_not_contain", "must_contain_turn1", "must_contain_turn2"))]
    fact_denominator = len(fact_cases)
    fact_numerator = len(fact_pass)

    # 指标 2：引用命中率（知识问答命中）
    kq_cases = [r for r in records if r["category"] == "知识问答命中"]
    kq_pass = [r for r in kq_cases if r["text_checks"].get("citations", {}).get("ok")]

    # 指标 3：越权拒绝率
    finx_cases = [r for r in records if r["category"] == "财务越权"]
    finx_pass = [r for r in finx_cases if r["forbidden_triplet"] and r["forbidden_triplet"]["ok"]]
    fin_legit_cases = [r for r in records if r["category"] == "财务合法查询"]
    fin_legit_over_rejected = [r for r in fin_legit_cases if not r["text_checks"]["must_contain"]["ok"]]

    # 指标 4：无依据拒答率
    nohit_cases = [r for r in records if r["category"] == "知识库无命中"]
    nohit_pass = [
        r
        for r in nohit_cases
        if r["text_checks"].get("must_contain", {}).get("ok") and r["text_checks"].get("citations", {}).get("ok")
    ]

    # 指标 5：少 AI 味评分
    scores = [r["ai_flavor"]["score"] for r in records]
    avg_score = sum(scores) / len(scores) if scores else 0
    above_80 = sum(1 for s in scores if s >= 80)

    # 指标 6：转人工准确率
    handoff_pass = [r for r in records if r["text_checks"]["handoff"]["ok"]]
    over_transferred = [r for r in records if r["text_checks"]["handoff"]["got"] and not r["text_checks"]["handoff"]["want"]]
    missed_transferred = [r for r in records if r["text_checks"]["handoff"]["want"] and not r["text_checks"]["handoff"]["got"]]

    failures = [r for r in records if not r["overall_ok"]]

    return {
        "fact_accuracy": {"numerator": fact_numerator, "denominator": fact_denominator, "ratio": _agg_ratio(fact_numerator, fact_denominator)},
        "citation_hit_rate": {"numerator": len(kq_pass), "denominator": len(kq_cases), "ratio": _agg_ratio(len(kq_pass), len(kq_cases))},
        "forbidden_reject_rate": {"numerator": len(finx_pass), "denominator": len(finx_cases), "ratio": _agg_ratio(len(finx_pass), len(finx_cases))},
        "legit_over_rejected": {"numerator": len(fin_legit_over_rejected), "denominator": len(fin_legit_cases), "ids": [r["id"] for r in fin_legit_over_rejected]},
        "no_basis_refusal_rate": {"numerator": len(nohit_pass), "denominator": len(nohit_cases), "ratio": _agg_ratio(len(nohit_pass), len(nohit_cases))},
        "ai_flavor": {"average": round(avg_score, 1), "above_80_ratio": _agg_ratio(above_80, len(scores))},
        "handoff_accuracy": {
            "numerator": len(handoff_pass),
            "denominator": len(records),
            "ratio": _agg_ratio(len(handoff_pass), len(records)),
            "over_transferred_ids": [r["id"] for r in over_transferred],
            "missed_transferred_ids": [r["id"] for r in missed_transferred],
        },
        "failures": [r["id"] for r in failures],
    }


async def collect_token_usage() -> dict:
    usage = {}
    async with AsyncSessionLocal() as session:
        for tenant_id in TENANTS_TO_CHECK:
            result = await session.execute(
                select(LlmUsage).where(LlmUsage.tenant_id == tenant_id, LlmUsage.created_at >= _RUN_START)
            )
            rows = result.scalars().all()
            prompt = sum(r.prompt_tokens for r in rows)
            completion = sum(r.completion_tokens for r in rows)
            usage[tenant_id] = {"prompt_tokens": prompt, "completion_tokens": completion, "calls": len(rows)}
    return usage


_RUN_START = datetime.now(timezone.utc)


# 相邻两题之间留出的最短间隔（PHASE5.md 5.4 原文"顺序发送，不绕过限流"要求真实按顺序发送，
# 不能把 50 题拆开并发绕开限流；但这不等于要把评测脚本自己的发送速度压到刚好卡在限流阈值上——
# u_a_1001 一个用户就占了 50 题里的大多数，脚本发得比真实用户快很多，实测不加间隔时到第 40
# 题左右会连续撞上 RATE_LIMIT_USER_PER_10S=20（同一个用户 10 秒内最多 20 条），
# 触发的是"评测脚本自己发太快"，不是被测系统的真实缺陷，跟这 50 道题任何一道要测的能力都无关。
# 加一个不算长的间隔，让整个运行节奏更接近"一个人依次发消息"，不是给限流开后门。
INTER_CASE_DELAY_SECONDS = 1.0


async def main(only_ids: list[str] | None = None) -> None:
    global _RUN_START
    _RUN_START = datetime.now(timezone.utc)
    await preflight()

    cases = load_cases()
    if only_ids:
        wanted = set(only_ids)
        cases = [c for c in cases if c["id"] in wanted]
    OUTPUT_DIR.mkdir(exist_ok=True)

    records: list[dict] = []
    async with AsyncSessionLocal() as session, httpx.AsyncClient() as client:
        await reset_eval_state(session, client, cases)
        for i, case in enumerate(cases, 1):
            record = await evaluate_case(case, session, client)
            records.append(record)
            status = "PASS" if record["overall_ok"] else "FAIL"
            print(f"[{i:02d}/{len(cases)}] [{status}] {case['id']}（{case['category']}）")
            with open(OUTPUT_DIR / f"{case['id']}.json", "w", encoding="utf-8") as f:
                json.dump(record, f, ensure_ascii=False, indent=2, default=str)
            if i < len(cases):
                await asyncio.sleep(INTER_CASE_DELAY_SECONDS)

    summary = aggregate(records)
    token_usage = await collect_token_usage()

    print("\n===== 汇总 =====")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("\n两家机构 token 用量（本次评测期间）：")
    print(json.dumps(token_usage, ensure_ascii=False, indent=2))

    with open(OUTPUT_DIR / "summary.json", "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "token_usage": token_usage}, f, ensure_ascii=False, indent=2, default=str)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="LLM 质量评测")
    parser.add_argument(
        "--ids", default=None, help="只跑指定的题目 id（逗号分隔），调试用；不传就跑全部 50 条"
    )
    parsed = parser.parse_args()
    id_filter = parsed.ids.split(",") if parsed.ids else None

    try:
        asyncio.run(main(id_filter))
    except PreflightError as exc:
        print(f"[preflight 失败] {exc}", file=sys.stderr)
        sys.exit(1)
