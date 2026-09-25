"""WebSocket 消息协议。客户端内容只能出现在这些结构化字段里，网关校验完才会往下传。"""
from typing import Any, Literal, Optional

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
    status: Literal["accepted", "duplicate", "rate_limited"]
    trace_id: str
    # 只有 rate_limited 会带这句话，客户端直接显示；accepted/duplicate 不需要额外文案
    detail: Optional[str] = None


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
    # 调试/测试/阶段三演示控制台用：意图、路由来源、工具调用情况、引用出处等，见 PHASE2.md 1.9
    meta: Optional[dict[str, Any]] = None


class ErrorMessage(BaseModel):
    """gateway -> 客户端：出错了"""

    type: Literal["error"] = "error"
    message_id: Optional[str] = None
    code: str
    detail: str


class ReminderPushMessage(BaseModel):
    """scheduler -> 客户端：提醒到点推送。跟对话回复（reply_chunk/reply_end）走同一个 Redis
    频道、同一条 gateway 转发链路，但 type 不同——客户端（阶段三第 7 步的演示控制台）靠这个
    字段区分"这是一条提醒"还是"这是一条对话回复"，用不同样式展示。
    """

    type: Literal["reminder"] = "reminder"
    reminder_id: str
    title: str
    text: str
