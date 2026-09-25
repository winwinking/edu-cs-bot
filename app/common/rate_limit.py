"""Redis 计数限流（PHASE3.md 第 4 步，设计决定 8、9）。

用 Lua 脚本把"加一"和"首次设过期"合成一个原子操作：不用 Lua 的话，INCR 和 EXPIRE 是两条
命令，中间可能被别的请求插队，或者进程崩在两条命令之间，导致某个 key 永远没有过期时间、
一直往上加。

Redis 报错时直接放行（设计决定 9）：限流组件本身坏了，不能把所有用户都挡在外面。
"""
from redis.exceptions import RedisError

from app.common.config import get_settings
from app.common.logging import get_logger
from app.common.redis import note_redis_result, redis_client

logger = get_logger(__name__)
settings = get_settings()

# KEYS[1]=计数 key，ARGV[1]=窗口秒数；只有这一次调用让计数从 0 变 1 时才设过期，避免每次调用
# 都重新把过期时间往后推（那样窗口会变成"最近一次请求之后再等 N 秒"，不是"固定 N 秒窗口"）
_INCR_WITH_EXPIRE_SCRIPT = """
local current = redis.call("INCR", KEYS[1])
if tonumber(current) == 1 then
    redis.call("EXPIRE", KEYS[1], ARGV[1])
end
return current
"""

_incr_with_expire = redis_client.register_script(_INCR_WITH_EXPIRE_SCRIPT)


async def check_rate_limit(key: str, limit: int, window_seconds: int) -> bool:
    """返回 True 表示允许通过。Redis 调用失败也返回 True（放行），并记一次 Redis 降级状态。"""
    try:
        current = await _incr_with_expire(keys=[key], args=[window_seconds])
        note_redis_result(True)
    except RedisError as exc:
        note_redis_result(False)
        logger.warning("限流检查调用 Redis 失败，放行", key=key, error=str(exc))
        return True
    return int(current) <= limit


async def check_user_and_tenant_rate_limit(tenant_id: str, user_id: str) -> bool:
    """gateway 用：按用户和按机构各查一次，任意一个超限就算超限。用户维度的 key 带滑动窗口秒数
    （10 秒），机构维度是 1 秒——两个窗口长度不同，不能共用同一个 key 命名，也不能合并成一次
    Redis 调用。"""
    user_key = f"ratelimit:user:{tenant_id}:{user_id}"
    tenant_key = f"ratelimit:tenant:{tenant_id}"
    user_ok = await check_rate_limit(user_key, settings.rate_limit_user_per_10s, 10)
    tenant_ok = await check_rate_limit(tenant_key, settings.rate_limit_tenant_per_sec, 1)
    return user_ok and tenant_ok
