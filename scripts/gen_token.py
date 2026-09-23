"""打印一个有效 JWT，供手工测试用。

用法：python scripts/gen_token.py --tenant t_a --user u_a_1001

role 从数据库里查，不用手动传，避免手工测试时传错角色和数据库不一致。
"""
import argparse
import asyncio

from sqlalchemy import select

from app.common.auth import create_access_token
from app.common.db import AsyncSessionLocal
from app.common.models import User


async def main(tenant: str, user: str) -> None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.tenant_id == tenant, User.id == user))
        row = result.scalar_one_or_none()
        if row is None:
            raise SystemExit(f"用户不存在：tenant={tenant} user={user}，先跑 make seed")
        token = create_access_token(user_id=row.id, tenant_id=row.tenant_id, role=row.role.value)
    print(token)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--user", required=True)
    args = parser.parse_args()
    asyncio.run(main(args.tenant, args.user))
