"""种子数据：两个租户，每个租户 3 个用户，覆盖 student/parent/agent 角色。可重复运行。"""
import asyncio

from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.common.db import AsyncSessionLocal
from app.common.models import Tenant, User, UserRole

TENANTS = [
    {"id": "t_a", "name": "星辰教育"},
    {"id": "t_b", "name": "启明学堂"},
]

USERS = [
    {
        "id": "u_a_1001",
        "tenant_id": "t_a",
        "name": "张小明",
        "role": UserRole.student,
        "email": "u_a_1001@example.com",
        "phone": "13800001001",
    },
    {
        "id": "u_a_1002",
        "tenant_id": "t_a",
        "name": "张爸爸",
        "role": UserRole.parent,
        "email": "u_a_1002@example.com",
        "phone": "13800001002",
    },
    {
        "id": "u_a_1003",
        "tenant_id": "t_a",
        "name": "客服小李",
        "role": UserRole.agent,
        "email": "u_a_1003@example.com",
        "phone": "13800001003",
    },
    {
        "id": "u_b_1001",
        "tenant_id": "t_b",
        "name": "李小红",
        "role": UserRole.student,
        "email": "u_b_1001@example.com",
        "phone": "13800002001",
    },
    {
        "id": "u_b_1002",
        "tenant_id": "t_b",
        "name": "李妈妈",
        "role": UserRole.parent,
        "email": "u_b_1002@example.com",
        "phone": "13800002002",
    },
    {
        "id": "u_b_1003",
        "tenant_id": "t_b",
        "name": "客服小王",
        "role": UserRole.agent,
        "email": "u_b_1003@example.com",
        "phone": "13800002003",
    },
]


async def main() -> None:
    async with AsyncSessionLocal() as session:
        # ON CONFLICT DO NOTHING：种子脚本要能重复跑，不能因为已经种过就报唯一约束冲突
        for tenant in TENANTS:
            stmt = pg_insert(Tenant).values(**tenant).on_conflict_do_nothing(index_elements=["id"])
            await session.execute(stmt)
        for user in USERS:
            stmt = pg_insert(User).values(**user).on_conflict_do_nothing(index_elements=["id"])
            await session.execute(stmt)
        await session.commit()
    print(f"种子数据完成：{len(TENANTS)} 个租户，{len(USERS)} 个用户")


if __name__ == "__main__":
    asyncio.run(main())
