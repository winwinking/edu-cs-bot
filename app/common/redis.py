"""redis.asyncio 客户端，全项目共用一个连接池。"""
from redis import asyncio as aioredis

from app.common.alerts import raise_alert
from app.common.config import get_settings
from app.common.logging import get_logger

settings = get_settings()
logger = get_logger(__name__)

# socket_timeout / socket_connect_timeout 保证 Redis 调用不会无限等待（硬性规则：外部调用必须有超时）
redis_client = aioredis.from_url(
    settings.redis_url,
    password=settings.redis_password or None,
    decode_responses=True,
    socket_timeout=settings.redis_timeout_seconds,
    socket_connect_timeout=settings.redis_timeout_seconds,
)


async def check_redis_connection() -> bool:
    """给 /ready 探活用"""
    try:
        return bool(await redis_client.ping())
    except Exception:
        return False


# 进程级别的"Redis 好不好用"状态：去重、限流、发布/订阅这些地方各自调用 Redis 失败时都上报到
# 这一个共用状态，只在"可用→不可用"和"不可用→恢复"两个转折点各打一条日志（设计决定 9），
# 不是每次调用失败都打一条——那样 Redis 挂几分钟能把日志刷屏
_redis_available = True


def note_redis_result(ok: bool) -> None:
    global _redis_available
    if ok and not _redis_available:
        logger.info("Redis 已恢复")
        _redis_available = True
    elif not ok and _redis_available:
        logger.warning("Redis 不可用，已降级")
        raise_alert("redis_unavailable")
        _redis_available = False
