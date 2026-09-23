"""单条入站消息的业务处理：校验会话归属 -> 去重插入 -> 读取上下文 -> 调 LLM -> 写回复 -> 推流式分片。"""
import time
import uuid
from typing import List, Optional

from openai import APIConnectionError, APIError, APITimeoutError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from app.common.config import get_settings
from app.common.db import AsyncSessionLocal
from app.common.llm_client import stream_chat_completion
from app.common.logging import get_logger
from app.common.models import Conversation, Message, MessageRole

from app.worker.metrics import first_token_seconds
from app.worker.pubsub import publish_error, publish_reply_chunk, publish_reply_end

settings = get_settings()
logger = get_logger(__name__)

_DEGRADED_REPLY = "系统有点忙，我稍后再回复你，也可以回复'转人工'"


class ConversationForbidden(Exception):
    """会话存在，但归属的 tenant_id/user_id 跟当前消息对不上——业务上的越权，不是 bug"""


async def _resolve_conversation(session, tenant_id: str, user_id: str, conversation_id_raw: str) -> uuid.UUID:
    # 格式不对（不是合法 UUID）会在这里抛 ValueError，交给上层当"不可预期异常"处理进死信——
    # 能发出格式错误 conversation_id 的只有坏客户端或消息损坏，不是我们要兜底的业务场景
    conversation_id = uuid.UUID(conversation_id_raw)

    result = await session.execute(select(Conversation).where(Conversation.id == conversation_id))
    conversation = result.scalar_one_or_none()

    if conversation is None:
        conversation = Conversation(id=conversation_id, tenant_id=tenant_id, user_id=user_id)
        session.add(conversation)
        try:
            await session.flush()
            return conversation_id
        except IntegrityError:
            # 两个 worker 几乎同时创建同一个新会话的极端情况：谁先谁后不重要，退回去当"已存在"处理
            await session.rollback()
            result = await session.execute(select(Conversation).where(Conversation.id == conversation_id))
            conversation = result.scalar_one()

    if conversation.tenant_id != tenant_id or conversation.user_id != user_id:
        raise ConversationForbidden()
    return conversation_id


async def _insert_user_message(
    session, tenant_id: str, conversation_id: uuid.UUID, client_message_id: str, content: str, trace_id: Optional[str]
) -> bool:
    # ON CONFLICT DO NOTHING + RETURNING：插成功了才会有返回行，插突不成功说明这条消息已经处理过，
    # 这是"队列至少一次投递 + 数据库唯一约束 = 业务只处理一次"里，数据库这一侧的实现
    stmt = (
        pg_insert(Message)
        .values(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            message_id=client_message_id,
            role=MessageRole.user,
            content=content,
            trace_id=trace_id,
        )
        .on_conflict_do_nothing(constraint="uq_messages_tenant_message_id")
        .returning(Message.id)
    )
    result = await session.execute(stmt)
    return result.first() is not None


async def _load_recent_messages(session, tenant_id: str, conversation_id: uuid.UUID, limit: int) -> List[dict]:
    stmt = (
        select(Message.role, Message.content)
        .where(Message.tenant_id == tenant_id, Message.conversation_id == conversation_id)
        .order_by(Message.created_at.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    rows = list(result.all())
    rows.reverse()  # 数据库按时间倒序取出的，喂给 LLM 前要反转成"旧的在前，新的在后"
    return [{"role": row.role.value, "content": row.content} for row in rows]


async def _insert_assistant_message(
    session, tenant_id: str, conversation_id: uuid.UUID, content: str, trace_id: Optional[str]
) -> None:
    session.add(
        Message(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            # assistant 回复不是客户端发的，没有客户端 message_id，自己生成一个占位的，
            # 反正 (tenant_id, message_id) 唯一约束只需要保证不撞车
            message_id=f"assistant-{uuid.uuid4()}",
            role=MessageRole.assistant,
            content=content,
            trace_id=trace_id,
        )
    )


async def _stream_and_publish_reply(tenant_id: str, user_id: str, reply_to: str, history: List[dict]) -> str:
    start = time.monotonic()
    first_token_seen = False
    chunks: List[str] = []
    seq = 0
    async for delta in stream_chat_completion(history):
        if not first_token_seen:
            first_token_seconds.observe(time.monotonic() - start)
            first_token_seen = True
        chunks.append(delta)
        await publish_reply_chunk(tenant_id, user_id, reply_to, seq, delta)
        seq += 1
    await publish_reply_end(tenant_id, user_id, reply_to)
    return "".join(chunks)


async def process_inbound_message(
    *,
    tenant_id: str,
    user_id: str,
    conversation_id_raw: str,
    client_message_id: str,
    content: str,
    trace_id: Optional[str],
) -> str:
    """返回本次处理结果的标签，只用来打指标。

    所有"预期内"的情况（越权、去重命中、LLM 失败）都在这里处理完并正常返回，不往外抛异常；
    真正往外抛的异常（数据库挂了、conversation_id 格式非法等）由上层（consumer）判定为
    不可预期异常，reject 进死信，这里不用关心队列层面的 ack/reject。
    """
    async with AsyncSessionLocal() as session:
        try:
            conversation_id = await _resolve_conversation(session, tenant_id, user_id, conversation_id_raw)
        except ConversationForbidden:
            await session.rollback()
            await publish_error(tenant_id, user_id, client_message_id, "forbidden", "该会话不属于当前用户")
            logger.warning("越权访问会话", conversation_id=conversation_id_raw)
            return "forbidden"

        inserted = await _insert_user_message(
            session, tenant_id, conversation_id, client_message_id, content, trace_id
        )
        if not inserted:
            await session.commit()
            logger.info("消息已处理过（命中数据库唯一约束），跳过", message_id=client_message_id)
            return "duplicate"

        history = await _load_recent_messages(
            session, tenant_id, conversation_id, settings.conversation_history_limit
        )
        await session.commit()

    # LLM 调用挪到 session 外面：流式请求可能要好几秒，不能一直占着数据库连接
    try:
        reply_text = await _stream_and_publish_reply(tenant_id, user_id, client_message_id, history)
        result = "ok"
    except (APIError, APITimeoutError, APIConnectionError) as exc:
        # 可预期的失败：LLM 超时/报错，推一条降级回复给用户，不进死信
        reply_text = _DEGRADED_REPLY
        await publish_reply_chunk(tenant_id, user_id, client_message_id, 0, reply_text)
        await publish_reply_end(tenant_id, user_id, client_message_id)
        logger.warning("LLM 调用失败，已发送降级回复", error=str(exc))
        result = "llm_degraded"

    async with AsyncSessionLocal() as session:
        await _insert_assistant_message(session, tenant_id, conversation_id, reply_text, trace_id)
        await session.commit()

    return result
