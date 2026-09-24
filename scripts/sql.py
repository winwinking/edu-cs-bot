"""只读 SQL 查询工具：只允许 SELECT（含 WITH ... SELECT），其他语句一律拒绝执行。

给 Jo 手工验证数据库状态用，不接入任何业务代码。

用法：python scripts/sql.py "select id, tenant_id, role from users order by id"
"""
import argparse
import asyncio

from sqlalchemy import text

from app.common.db import AsyncSessionLocal

_ALLOWED_LEADING_KEYWORDS = ("select", "with")
# 手工查询工具，给足时间但不能无限等（硬性规则：外部调用必须有超时）
_QUERY_TIMEOUT_SECONDS = 30


def _check_readonly(sql: str) -> str:
    stripped = sql.strip().rstrip(";")
    if not stripped:
        raise SystemExit("拒绝执行：空语句")
    if ";" in stripped:
        raise SystemExit("拒绝执行：不支持一次执行多条语句")
    first_word = stripped.split(None, 1)[0].lower()
    if first_word not in _ALLOWED_LEADING_KEYWORDS:
        raise SystemExit(f"拒绝执行：只允许 SELECT 查询，收到的语句以 '{first_word}' 开头")
    return stripped


async def main(sql: str) -> None:
    checked_sql = _check_readonly(sql)
    async with AsyncSessionLocal() as session:
        async with session.begin():
            # 数据库层面再加一道防线：即使上面的关键字检查有漏判，这个事务也写不进任何东西
            await session.execute(text("SET TRANSACTION READ ONLY"))
            async with asyncio.timeout(_QUERY_TIMEOUT_SECONDS):
                result = await session.execute(text(checked_sql))
            rows = result.fetchall()
            columns = list(result.keys())

    print("\t".join(columns))
    for row in rows:
        print("\t".join("" if v is None else str(v) for v in row))
    print(f"({len(rows)} 行)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="只读 SQL 查询工具，只允许 SELECT")
    parser.add_argument("sql", help="要执行的 SELECT 语句")
    args = parser.parse_args()
    asyncio.run(main(args.sql))
