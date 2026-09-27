"""压测用户生成脚本（PHASE4.md 4.6，关键设计决定 6）。

k6 发消息走真实的 WebSocket -> gateway -> worker 全链路，worker 处理消息时会按
(tenant_id, conversation_id) 找/建 `conversations` 行，`conversations.user_id` 是
`ForeignKey("users.id")` 且 `nullable=False`（见 app/common/models.py）——如果 token 里的
user_id 在 `users` 表里不存在，第一次建会话就会因为外键约束失败报错，压测量出来的全是"系统跟
不知道哪来的错"而不是真实性能。所以压测用户必须是 `users` 表里真实存在的行，不能只签一个 JWT
就当作有效用户，这跟 `scripts/seed.py` 只种 7 个人是两回事——那是给功能测试用的，这里要的是
数量（PHASE4.md 要求 t_a 下至少 1500 个）。

跟 `scripts/seed.py` 一样用 `ON CONFLICT DO NOTHING` 让脚本可以重复跑：已经种过的这次再跑
不会报唯一约束冲突，也不会覆盖掉已有行。

生成的 token 是明文 JWT，写进 `loadtest/tokens.json`，这个文件在 `.gitignore` 里，绝不能提交。

用法（本轮 PHASE4.md 4.6 只做准备，不实际执行；真正跑压测前才手动跑一次）：
  docker compose run --rm tools python loadtest/gen_users.py --tenant t_a --count 1600
"""
import argparse
import asyncio
import json

from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.common.auth import create_access_token
from app.common.db import AsyncSessionLocal
from app.common.models import User, UserRole

# 一批 INSERT 太多行容易踩 PostgreSQL 单条语句参数上限，也不好排查是哪一批出的错，
# 分批跟 scripts/seed.py 的风格保持一致（那边数据量小不用分批，这里量大了才需要）
_BATCH_SIZE = 500

# token 有效期给足（分钟）：压测跑得比这个短就行，默认 6 小时覆盖"生成 token 之后隔了一会儿
# 才开始跑 k6"这种正常操作延迟，不需要跟真实用户登录态的 JWT_EXPIRE_MINUTES 共用同一个值
_TOKEN_EXPIRE_MINUTES = 360


def _user_id(tenant: str, index: int) -> str:
    # loadtest_ 前缀跟 seed.py 里真实业务用户（u_a_1001 这种）的命名一望而知能区分开，
    # 排障时一眼就能看出这是压测数据不是真实数据
    return f"loadtest_{tenant}_{index:06d}"


def _build_users(tenant: str, count: int) -> list[dict]:
    return [
        {
            "id": _user_id(tenant, i),
            "tenant_id": tenant,
            "name": f"压测用户{i}",
            "role": UserRole.student,
            "email": f"loadtest+{tenant}_{i:06d}@example.com",
            "phone": None,
        }
        for i in range(1, count + 1)
    ]


async def main(tenant: str, count: int, out_path: str) -> None:
    users = _build_users(tenant, count)

    async with AsyncSessionLocal() as session:
        for start in range(0, len(users), _BATCH_SIZE):
            batch = users[start : start + _BATCH_SIZE]
            stmt = pg_insert(User).values(batch).on_conflict_do_nothing(index_elements=["id"])
            await session.execute(stmt)
        await session.commit()

    # 签 token 不用查数据库确认角色（跟 gen_token.py 不一样）：上面已经用同样的
    # role=student 把这些用户写进了数据库，这里直接复用同一个值，不需要多一次往返查询
    tokens = [
        {"user_id": u["id"], "token": create_access_token(user_id=u["id"], tenant_id=tenant, role="student", expire_minutes=_TOKEN_EXPIRE_MINUTES)}
        for u in users
    ]
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(tokens, f, ensure_ascii=False)

    print(f"已生成/确认 {len(users)} 个压测用户（tenant={tenant}），token 写入 {out_path}")
    print(f"token 有效期 {_TOKEN_EXPIRE_MINUTES} 分钟，过期后重新跑这个脚本即可（用户行是幂等的，不会重复插入）")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="生成压测用户 + token 文件")
    parser.add_argument("--tenant", default="t_a")
    parser.add_argument("--count", type=int, default=1600, help="PHASE4.md 要求至少 1500，默认多留一点余量")
    parser.add_argument("--out", default="loadtest/tokens.json")
    args = parser.parse_args()
    asyncio.run(main(args.tenant, args.count, args.out))
