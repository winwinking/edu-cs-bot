"""种子数据：两个租户，每个租户 3 个用户，覆盖 student/parent/agent 角色。可重复运行。"""
import asyncio

from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.common.db import AsyncSessionLocal
from app.common.models import GuardianLink, Tenant, User, UserRole

TENANTS = [
    # 阶段四 4.1：给两个机构设不同的每日 token 预算，演示控制台的状态条才有"用量 / 上限"可显示；
    # t_b 的预算比 t_a 小很多，方便压测/演示时更容易触发预算降级
    {
        "id": "t_a",
        "name": "星辰教育",
        "service_hours": "9:00 至 21:00",
        "timezone": "Asia/Shanghai",
        "daily_token_budget": 2_000_000,
    },
    {
        "id": "t_b",
        "name": "启明学堂",
        "service_hours": "8:30 至 20:30",
        "timezone": "Asia/Shanghai",
        "daily_token_budget": 500_000,
    },
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
    {
        # 阶段二新增：和张小明没有关联的学生，用来演示"学生 A 查学生 B"被拒绝
        "id": "u_a_1004",
        "tenant_id": "t_a",
        "name": "王小华",
        "role": UserRole.student,
        "email": "u_a_1004@example.com",
        "phone": "13800001004",
    },
]

# 家长-学员关联（阶段二）：家长能查关联学员的财务，见 app/common/models.py 的 GuardianLink
GUARDIAN_LINKS = [
    {"tenant_id": "t_a", "parent_user_id": "u_a_1002", "student_user_id": "u_a_1001"},
    {"tenant_id": "t_b", "parent_user_id": "u_b_1002", "student_user_id": "u_b_1001"},
]


async def main() -> None:
    async with AsyncSessionLocal() as session:
        # tenants 改成 ON CONFLICT DO NOTHING（5.1 全新克隆审查后改的：make up 现在会自动跑
        # seed，机构配置必须"只在首次创建时写入，之后不再覆盖"，否则压测/演示期间手动改过的
        # daily_token_budget 等字段会被每次 make up 悄悄冲回种子默认值。这跟"改代码里的种子
        # 默认值想推给老环境"是两回事——后面这种需求如果出现，应该用一次性的迁移或运维脚本去改
        # 已有机构的配置，不应该让常驻的 make up 顺手做这件事。
        for tenant in TENANTS:
            stmt = pg_insert(Tenant).values(**tenant).on_conflict_do_nothing(index_elements=["id"])
            await session.execute(stmt)
        # users/guardian_links 同样用 ON CONFLICT DO NOTHING：种子脚本要能重复跑，不能因为已经
        # 种过就报唯一约束冲突，也不需要每次都覆盖成种子里的值
        for user in USERS:
            stmt = pg_insert(User).values(**user).on_conflict_do_nothing(index_elements=["id"])
            await session.execute(stmt)
        for link in GUARDIAN_LINKS:
            stmt = pg_insert(GuardianLink).values(**link).on_conflict_do_nothing(
                index_elements=["tenant_id", "parent_user_id", "student_user_id"]
            )
            await session.execute(stmt)
        await session.commit()
    print(f"种子数据完成：{len(TENANTS)} 个租户，{len(USERS)} 个用户，{len(GUARDIAN_LINKS)} 条家长-学员关联")


if __name__ == "__main__":
    asyncio.run(main())
