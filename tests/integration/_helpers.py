"""tests/integration 共用的小工具：连真实 gateway/DB/RabbitMQ 发消息、查 token。

跟 scripts/phase2_smoke.py、scripts/phase3_smoke.py 的写法是同一套（这两个脚本本身也是
"真实起 docker compose"这条检验路径），这里抽出来给 pytest 用例复用，避免每个测试文件
都抄一遍连接逻辑。tests/integration 依赖 `make up` 起的真实 PostgreSQL/Redis/RabbitMQ/
gateway/worker/mock-*，不是 tests/unit 那种不连服务的纯函数测试。
"""
import asyncio
import json
import uuid

import httpx
import websockets
from sqlalchemy import select

from app.common.auth import create_access_token
from app.common.db import AsyncSessionLocal
from app.common.config import get_settings
from app.common.models import User

settings = get_settings()

GATEWAY_URL = "ws://gateway:8000/ws"
RABBITMQ_API_BASE = "http://rabbitmq:15672/api"
REPLY_TIMEOUT_SECONDS = 30

_CONVERSATION_NAMESPACE = uuid.UUID("6f8f2c2e-1a4b-4e9a-9f1a-2c9a7e6d5b4a")


def conversation_id(tenant: str, user: str, conv_label: str) -> str:
    return str(uuid.uuid5(_CONVERSATION_NAMESPACE, f"{tenant}:{user}:{conv_label}"))


async def get_token(tenant: str, user: str) -> str:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.tenant_id == tenant, User.id == user))
        row = result.scalar_one_or_none()
        if row is None:
            raise RuntimeError(f"用户不存在：tenant={tenant} user={user}，先跑 make seed")
        return create_access_token(user_id=row.id, tenant_id=row.tenant_id, role=row.role.value)


async def send_and_wait(tenant: str, user: str, conv_label: str, content: str, message_id: str | None = None) -> dict:
    """发一条消息，等到 ack，再等到 reply_end（ack=duplicate/rate_limited 提前返回）。"""
    token = await get_token(tenant, user)
    conv_id = conversation_id(tenant, user, conv_label)
    mid = message_id or str(uuid.uuid4())

    async with websockets.connect(f"{GATEWAY_URL}?token={token}") as ws:
        await ws.send(
            json.dumps({"type": "message", "message_id": mid, "conversation_id": conv_id, "content": content})
        )
        ack = json.loads(await ws.recv())
        if ack["status"] in ("duplicate", "rate_limited"):
            return {"ack": ack["status"], "reply": "", "meta": {}, "message_id": mid, "trace_id": ack.get("trace_id")}

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
                        return {
                            "ack": ack["status"],
                            "reply": "",
                            "meta": {"error": msg},
                            "message_id": mid,
                            "trace_id": ack.get("trace_id"),
                        }
        except TimeoutError:
            raise RuntimeError(f"超过 {REPLY_TIMEOUT_SECONDS} 秒没收到 reply_end")

        return {
            "ack": ack["status"],
            "reply": "".join(chunks),
            "meta": meta,
            "message_id": mid,
            "trace_id": ack.get("trace_id"),
        }


async def rabbitmq_queue_stats(queue: str) -> dict:
    """查 RabbitMQ management API 里某个队列的统计信息（消息数、发布/确认累计计数等）。"""
    auth = httpx.BasicAuth(settings.rabbitmq_user, settings.rabbitmq_password)
    async with httpx.AsyncClient(timeout=5, auth=auth) as client:
        resp = await client.get(f"{RABBITMQ_API_BASE}/queues/%2F/{queue}")
        resp.raise_for_status()
        return resp.json()


def publish_count(stats: dict) -> int:
    return stats.get("message_stats", {}).get("publish", 0)


def ack_count(stats: dict) -> int:
    return stats.get("message_stats", {}).get("ack", 0)


async def set_mock_mode(service: str, **updates) -> None:
    async with httpx.AsyncClient(timeout=5) as client:
        resp = await client.post(f"http://mock-{service}:8000/admin/config", json=updates)
        resp.raise_for_status()


async def reset_mock(service: str) -> None:
    async with httpx.AsyncClient(timeout=5) as client:
        resp = await client.post(f"http://mock-{service}:8000/admin/reset")
        resp.raise_for_status()
