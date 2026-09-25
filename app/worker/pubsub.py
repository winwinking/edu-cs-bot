"""worker 往 gateway 订阅的 Redis 频道推消息，gateway 收到后原样转发给对应用户的 WebSocket 连接。

Redis 发布失败时不崩溃、不往外抛异常（PHASE3.md 第 4 步，设计决定 9）：回复已经生成好了，
该存库的、该标 replied 的都照样做，用户只是这一次没能实时收到推送，不是整条消息处理失败——
不能因为"推不出去"就让 worker 把这条本来处理成功的消息当成异常重新走一遍死信重试流程。
"""
from typing import Any, Optional

from redis.exceptions import RedisError

from app.common.logging import get_logger
from app.common.redis import note_redis_result, redis_client
from app.common.schemas import ErrorMessage, ReplyChunkMessage, ReplyEndMessage

logger = get_logger(__name__)


def _channel(tenant_id: str, user_id: str) -> str:
    return f"im:out:{tenant_id}:{user_id}"


async def _publish(tenant_id: str, user_id: str, payload: str) -> None:
    try:
        await redis_client.publish(_channel(tenant_id, user_id), payload)
        note_redis_result(True)
    except RedisError as exc:
        note_redis_result(False)
        logger.warning("推送到 Redis 频道失败，已跳过", tenant_id=tenant_id, user_id=user_id, error=str(exc))


async def publish_reply_chunk(tenant_id: str, user_id: str, reply_to: str, seq: int, delta: str) -> None:
    msg = ReplyChunkMessage(reply_to=reply_to, seq=seq, delta=delta)
    await _publish(tenant_id, user_id, msg.model_dump_json())


async def publish_reply_end(
    tenant_id: str, user_id: str, reply_to: str, meta: Optional[dict[str, Any]] = None
) -> None:
    msg = ReplyEndMessage(reply_to=reply_to, meta=meta)
    await _publish(tenant_id, user_id, msg.model_dump_json())


async def publish_error(tenant_id: str, user_id: str, message_id: Optional[str], code: str, detail: str) -> None:
    msg = ErrorMessage(message_id=message_id, code=code, detail=detail)
    await _publish(tenant_id, user_id, msg.model_dump_json())
