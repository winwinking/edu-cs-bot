"""限流验证脚本（PHASE3.md 第 4 步）：10 秒内连发 30 条消息，打印每条的 ack 状态，
预期前 RATE_LIMIT_USER_PER_10S 条 accepted，之后的都是 rate_limited。

用法：
  docker compose run --rm tools python scripts/rate_limit_burst.py --tenant t_a --user u_a_1001
"""
import argparse
import asyncio
import json
import uuid

import websockets
from sqlalchemy import select

from app.common.auth import create_access_token
from app.common.db import AsyncSessionLocal
from app.common.models import User

_CONVERSATION_NAMESPACE = uuid.UUID("6f8f2c2e-1a4b-4e9a-9f1a-2c9a7e6d5b4a")


async def _get_token(tenant: str, user: str) -> str:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.tenant_id == tenant, User.id == user))
        row = result.scalar_one_or_none()
        if row is None:
            raise SystemExit(f"用户不存在：tenant={tenant} user={user}，先跑 make seed")
        return create_access_token(user_id=row.id, tenant_id=row.tenant_id, role=row.role.value)


async def run(tenant: str, user: str, count: int, gateway_url: str) -> None:
    token = await _get_token(tenant, user)
    conversation_id = str(uuid.uuid5(_CONVERSATION_NAMESPACE, f"{tenant}:{user}:rate_limit_burst"))
    url = f"{gateway_url}?token={token}"

    accepted = 0
    rate_limited = 0
    async with websockets.connect(url) as ws:
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
            status = ack.get("status")
            if status == "accepted":
                accepted += 1
            elif status == "rate_limited":
                rate_limited += 1
            print(f"[{i + 1}/{count}] status={status} detail={ack.get('detail')}")

    print(f"\n汇总：accepted={accepted} rate_limited={rate_limited}")


def main() -> None:
    parser = argparse.ArgumentParser(description="10 秒内连发多条消息，验证限流")
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--count", type=int, default=30)
    parser.add_argument("--gateway-url", default="ws://gateway:8000/ws")
    args = parser.parse_args()
    asyncio.run(run(args.tenant, args.user, args.count, args.gateway_url))


if __name__ == "__main__":
    main()
