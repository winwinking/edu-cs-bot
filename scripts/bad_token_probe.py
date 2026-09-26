"""故障注入 2「token 错误或过期」专用探针（PHASE4.md 4.5）：一条命令里现场签发一个坏 token
（过期或者签名对不上），再直接拿它去连 gateway，打印 gateway 的拒绝结果。

不拆成"先用 gen_token.py 生成 token，再用 ws_client.py 连"两条命令：Windows cmd 下要把
第一条命令的输出存进变量再喂给第二条命令，得用 `for /f`，比较绕，两步还容易在演示时敲错；
这里直接把"造一个坏 token"和"拿它连一次"写进同一个脚本，一条命令就能看到结果。正常 token
的连接验证还是用 scripts/gen_token.py + scripts/ws_client.py 这两个已有脚本，不用这个。

用法：
  docker compose run --rm tools python scripts/bad_token_probe.py --tenant t_a --user u_a_1001 --kind expired
  docker compose run --rm tools python scripts/bad_token_probe.py --tenant t_a --user u_a_1001 --kind invalid
"""
import argparse
import asyncio

import websockets
from sqlalchemy import select

from app.common.auth import create_access_token
from app.common.db import AsyncSessionLocal
from app.common.models import User

GATEWAY_URL = "ws://gateway:8000/ws"


async def _bad_token(tenant: str, user: str, kind: str) -> str:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.tenant_id == tenant, User.id == user))
        row = result.scalar_one_or_none()
        if row is None:
            raise SystemExit(f"用户不存在：tenant={tenant} user={user}，先跑 make seed")

    if kind == "expired":
        # expire_minutes 传负数：exp 算出来在过去，等价于"用户拿着一个昨天的 token 又来了"
        return create_access_token(user_id=row.id, tenant_id=row.tenant_id, role=row.role.value, expire_minutes=-1)

    # invalid：正常签发一个，再把结尾几位改掉，签名对不上，等价于"客户端传错/篡改了 token"
    good = create_access_token(user_id=row.id, tenant_id=row.tenant_id, role=row.role.value)
    return good[:-4] + ("aaaa" if not good.endswith("aaaa") else "bbbb")


async def main(tenant: str, user: str, kind: str) -> None:
    token = await _bad_token(tenant, user, kind)
    url = f"{GATEWAY_URL}?token={token}"
    async with websockets.connect(url) as ws:
        try:
            await ws.recv()
            print("意外：没有被拒绝，收到了正常消息（说明鉴权没生效，这是业务 bug，不是故障注入现象）")
        except websockets.exceptions.ConnectionClosed as exc:
            print(f"[{kind}] 连接被拒绝：close code={exc.code} reason={exc.reason!r}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="故障注入 2 专用：生成坏 token 并现场验证被拒绝")
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--kind", required=True, choices=["expired", "invalid"])
    args = parser.parse_args()
    asyncio.run(main(args.tenant, args.user, args.kind))
