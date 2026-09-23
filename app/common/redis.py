"""redis.asyncio 客户端，全项目共用一个连接池。"""
from redis import asyncio as aioredis

from app.common.config import get_settings

settings = get_settings()

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
