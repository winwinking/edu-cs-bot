"""mock-platform：假的平台指令系统（阶段二 2.10，附录 C 契约）。

跟 mock-finance 一样是"假的外部系统"，自己维护一份最小的订阅状态，不依赖 app.common。
两个关键设计：
- 幂等：POST /commands 用 idempotency_key 做去重，同一个 key 再来直接返回第一次的结果，
  不重新执行——worker 那边靠这个保证"超时重试"不会变成"指令执行两次"。
- 故障模拟只做在 POST /commands 上，不做在 GET /users/{id}/subscriptions 上：2.10 的故障演练是
  "确认后执行指令超时/失败"，如果连查订阅状态都模拟超时，第一句消息（生成确认话术那一步）就会
  先失败，压根走不到"重试执行指令"这条要验证的链路。查订阅状态永远正常返回，是故意的设计。
"""
import asyncio
import copy
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Literal, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

app = FastAPI(title="mock-platform")

_DEFAULT_CONFIG: dict = {
    "latency_ms": int(os.getenv("MOCK_PLATFORM_LATENCY_MS", "50")),
    "mode": os.getenv("MOCK_PLATFORM_MODE", "normal"),
    "agents_online": True,
}
_config: dict = dict(_DEFAULT_CONFIG)

# 每个用户名下的班课和自动续费状态，key 是 (tenant_id, user_id)。u_a_1001 的"春季数学班"开着
# 自动续费，跟 PHASE2.md 2.10 给的确认话术示例（"你要关闭的是"春季数学班"的自动续费"）对得上。
_DEFAULT_SUBSCRIPTIONS: dict[tuple[str, str], list[dict]] = {
    ("t_a", "u_a_1001"): [
        {"course_name": "春季数学班", "auto_renew": True},
        {"course_name": "口语提高班", "auto_renew": False},
    ],
    ("t_a", "u_a_1004"): [{"course_name": "暑期英语班", "auto_renew": False}],
    ("t_b", "u_b_1001"): [{"course_name": "春季英语班", "auto_renew": True}],
}
_SUBSCRIPTIONS: dict[tuple[str, str], list[dict]] = copy.deepcopy(_DEFAULT_SUBSCRIPTIONS)

# 幂等去重表：idempotency_key -> 第一次执行的响应结果
_IDEMPOTENCY_STORE: dict[str, dict] = {}
# 实际执行过的指令，供 GET /admin/commands 和 mockctl.py platform show-commands 用
_EXECUTED_COMMANDS: list[dict] = []
# 每个 idempotency_key 一把锁：slow_commit/timeout 模式下会真的有多个并发请求带着同一个
# idempotency_key 同时在途（worker 客户端超时放弃了，但服务端这边那个请求还在执行），如果只是
# "查表没有就执行"，没有锁保护，两个并发请求会各自都查到"没有"、各自都执行一遍，
# _EXECUTED_COMMANDS 里就会出现同一个 idempotency_key 两条记录——这才是真正违反幂等的地方，
# 不是"最终报了成功"那件事本身
_IDEMPOTENCY_LOCKS: dict[str, asyncio.Lock] = {}


class AdminConfigUpdate(BaseModel):
    latency_ms: Optional[int] = Field(default=None, ge=0)
    mode: Optional[Literal["normal", "timeout", "slow_commit", "error500"]] = None
    agents_online: Optional[bool] = None


class CommandRequest(BaseModel):
    tenant_id: str
    user_id: str
    action: str
    params: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/admin/config")
async def get_config() -> dict:
    return _config


@app.post("/admin/config")
async def update_config(update: AdminConfigUpdate) -> dict:
    if update.latency_ms is not None:
        _config["latency_ms"] = update.latency_ms
    if update.mode is not None:
        _config["mode"] = update.mode
    if update.agents_online is not None:
        _config["agents_online"] = update.agents_online
    return _config


@app.post("/admin/reset")
async def reset_config() -> dict:
    global _SUBSCRIPTIONS
    _config.update(_DEFAULT_CONFIG)
    _SUBSCRIPTIONS = copy.deepcopy(_DEFAULT_SUBSCRIPTIONS)
    _IDEMPOTENCY_STORE.clear()
    _EXECUTED_COMMANDS.clear()
    _IDEMPOTENCY_LOCKS.clear()
    return _config


async def _apply_mode_and_latency() -> None:
    if _config["mode"] == "timeout":
        # 永远不返回：模拟"平台这边压根没处理、客户端重试到用完也没等到任何结果"，用来验证
        # 重试耗尽后系统正确降级、回复失败话术，不会无限重试。跟下面 slow_commit 的区别是
        # slow_commit 最终会处理完，这个不会
        await asyncio.Event().wait()
        return
    if _config["mode"] == "slow_commit":
        # 睡 5 秒后正常继续执行——模拟真实世界里常见的"平台其实处理完了，只是响应比客户端超时
        # 还慢"：worker 客户端 3 秒等不到就放弃、按超时重试，但这次请求在服务端并没有真的失败，
        # 5 秒后会正常算出结果、按 idempotency_key 存进缓存表。后续重试请求用的是同一个
        # idempotency_key，只要在这条命令最终完成之后发过来，就会直接命中缓存拿到第一次的结果，
        # 不会导致平台重复执行——这正是幂等键要解决的问题，重试次数用完之前只要有一次重试的
        # 时间点晚于"服务端实际处理完"的时间点，用户最终看到的就是成功
        await asyncio.sleep(5)
        return
    if _config["mode"] == "error500":
        raise HTTPException(status_code=500, detail="mock-platform 模拟的上游错误（error500 模式）")
    await asyncio.sleep(_config["latency_ms"] / 1000)


def _execute(tenant_id: str, user_id: str, action: str, params: dict) -> tuple[str, dict]:
    if action in ("open_schedule", "query_study_report", "update_course_reminder"):
        return "success", {"action": action}

    if action in ("disable_auto_renew", "enable_auto_renew"):
        course_name = params.get("course_name")
        subs = _SUBSCRIPTIONS.get((tenant_id, user_id), [])
        target = next((c for c in subs if c["course_name"] == course_name), None)
        if target is None:
            return "failed", {"reason": "course_not_found", "course_name": course_name}
        target["auto_renew"] = action == "enable_auto_renew"
        return "success", {"course_name": course_name, "auto_renew": target["auto_renew"]}

    if action == "submit_leave":
        return "success", {"course_name": params.get("course_name"), "date": params.get("date")}

    return "failed", {"reason": "unknown_action", "action": action}


@app.post("/commands")
async def create_command(req: CommandRequest) -> dict:
    if req.idempotency_key in _IDEMPOTENCY_STORE:
        # 幂等命中：直接返回第一次的结果，不再模拟延迟/故障，也不再执行一次
        return _IDEMPOTENCY_STORE[req.idempotency_key]

    # slow_commit/timeout 模式下，worker 客户端超时放弃、带着同一个 idempotency_key 发起重试时，
    # 上一次那个请求很可能还没跑完（还在 _apply_mode_and_latency 里睡着）——这时候两个请求会
    # 同时看到"缓存里还没有"，如果不加锁会各自都执行一遍。用锁把"这个 key 正在被谁处理"序列化：
    # 后到的请求排队等第一个处理完，拿到的是同一份结果，而不是各自重新跑一次
    lock = _IDEMPOTENCY_LOCKS.setdefault(req.idempotency_key, asyncio.Lock())
    async with lock:
        # 拿到锁之后重新查一次：等锁的这段时间里，先拿到锁的那个请求可能已经执行完存好了结果
        if req.idempotency_key in _IDEMPOTENCY_STORE:
            return _IDEMPOTENCY_STORE[req.idempotency_key]

        await _apply_mode_and_latency()

        status, result = _execute(req.tenant_id, req.user_id, req.action, req.params)
        response = {"command_id": str(uuid.uuid4()), "status": status, "result": result}
        _IDEMPOTENCY_STORE[req.idempotency_key] = response
        _EXECUTED_COMMANDS.append(
            {
                **response,
                "tenant_id": req.tenant_id,
                "user_id": req.user_id,
                "action": req.action,
                "params": req.params,
                "idempotency_key": req.idempotency_key,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        return response


@app.get("/users/{user_id}/subscriptions")
async def get_user_subscriptions(user_id: str, tenant_id: str = Query(...)) -> dict:
    subs = _SUBSCRIPTIONS.get((tenant_id, user_id), [])
    return {"subscriptions": [dict(c) for c in subs]}


@app.get("/admin/commands")
async def list_commands() -> dict:
    return {"commands": _EXECUTED_COMMANDS}


@app.get("/agents/status")
async def agents_status() -> dict:
    if not _config["agents_online"]:
        return {"online": False, "queue_length": 0, "avg_wait_minutes": 0}
    return {"online": True, "queue_length": 3, "avg_wait_minutes": 5}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
