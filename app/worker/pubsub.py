"""worker 往 gateway 订阅的 Redis 频道推消息，gateway 收到后原样转发给对应用户的 WebSocket 连接。"""
from typing import Any, Optional

from app.common.redis import redis_client
from app.common.schemas import ErrorMessage, ReplyChunkMessage, ReplyEndMessage


def _channel(tenant_id: str, user_id: str) -> str:
    return f"im:out:{tenant_id}:{user_id}"


async def publish_reply_chunk(tenant_id: str, user_id: str, reply_to: str, seq: int, delta: str) -> None:
    msg = ReplyChunkMessage(reply_to=reply_to, seq=seq, delta=delta)
    await redis_client.publish(_channel(tenant_id, user_id), msg.model_dump_json())


async def publish_reply_end(
    tenant_id: str, user_id: str, reply_to: str, meta: Optional[dict[str, Any]] = None
) -> None:
    msg = ReplyEndMessage(reply_to=reply_to, meta=meta)
    await redis_client.publish(_channel(tenant_id, user_id), msg.model_dump_json())


async def publish_error(tenant_id: str, user_id: str, message_id: Optional[str], code: str, detail: str) -> None:
    msg = ErrorMessage(message_id=message_id, code=code, detail=detail)
    await redis_client.publish(_channel(tenant_id, user_id), msg.model_dump_json())
