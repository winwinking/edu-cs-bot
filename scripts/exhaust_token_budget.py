"""故障注入 17「机构 token 预算用完」专用小工具（PHASE4.md 4.5）：直接把某机构"今天"的用量
Redis key 写成一个超大值，不用真的连续发几百万字的对话才能把预算刷完。用的是
app.common.llm_usage 里跟真实判断逻辑同一份 `_budget_key()`（机构 id + 按机构时区算出的
当天日期），保证故障注入触发的判断路径和线上真实超预算走的是同一条代码，不是另外拍脑袋造一个
不对应的 key。

用法：
  docker compose run --rm tools python scripts/exhaust_token_budget.py --tenant t_a set
  docker compose run --rm tools python scripts/exhaust_token_budget.py --tenant t_a clear
"""
import argparse
import asyncio

from app.common.db import AsyncSessionLocal
from app.common.llm_usage import _budget_key
from app.common.models import Tenant
from app.common.redis import redis_client

_HUGE_USAGE = 999_999_999
_BUDGET_TTL_SECONDS = 2 * 24 * 3600


async def main(tenant: str, action: str) -> None:
    async with AsyncSessionLocal() as session:
        row = await session.get(Tenant, tenant)
        if row is None:
            raise SystemExit(f"机构不存在：{tenant}")
        timezone = row.timezone

    key = _budget_key(tenant, timezone)
    if action == "set":
        await redis_client.set(key, _HUGE_USAGE, ex=_BUDGET_TTL_SECONDS)
        print(f"已把 {key} 写成 {_HUGE_USAGE}，这个机构今天的 LLM 调用都会被判定超预算")
    else:
        await redis_client.delete(key)
        print(f"已删除 {key}，预算状态恢复正常")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="故障注入 17 专用：直接写 Redis 预算用量 key")
    parser.add_argument("--tenant", required=True)
    parser.add_argument("action", choices=["set", "clear"])
    args = parser.parse_args()
    asyncio.run(main(args.tenant, args.action))
