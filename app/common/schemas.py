"""WebSocket 消息协议。客户端内容只能出现在这些结构化字段里，网关校验完才会往下传。"""
from typing import Literal, Optional

from pydantic import BaseModel, field_validator

from app.common.config import get_settings

settings = get_settings()


class ClientMessage(BaseModel):
    """客户端 -> gateway"""

    type: Literal["message"]
    message_id: str
    conversation_id: str
    content: str

    @field_validator("content")
    @classmethod
    def _check_length(cls, v: str) -> str:
        # 超长直接在校验层拒绝，不占用后面的去重/入队逻辑
        if len(v) > settings.ws_message_max_length:
            raise ValueError(f"content 超过长度上限 {settings.ws_message_max_length}")
        return v


class AckMessage(BaseModel):
    """gateway -> 客户端：收到消息的确认"""

    type: Literal["ack"] = "ack"
    message_id: str
    status: Literal["accepted", "duplicate"]
    trace_id: str


class ReplyChunkMessage(BaseModel):
    """gateway -> 客户端：流式回复的一个分片"""

    type: Literal["reply_chunk"] = "reply_chunk"
    reply_to: str
    seq: int
    delta: str


class ReplyEndMessage(BaseModel):
    """gateway -> 客户端：流式回复结束"""

    type: Literal["reply_end"] = "reply_end"
    reply_to: str


class ErrorMessage(BaseModel):
    """gateway -> 客户端：出错了"""

    type: Literal["error"] = "error"
    message_id: Optional[str] = None
    code: str
    detail: str
