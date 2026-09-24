"""单条入站消息的业务处理：校验会话归属 -> 去重插入 -> 读取上下文 -> 跑 LangGraph 编排 -> 写回复。"""
import uuid
from typing import Any, List, Optional

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from app.common.config import get_settings
from app.common.db import AsyncSessionLocal
from app.common.logging import get_logger
from app.common.models import Conversation, Message, MessageRole, MessageStatus

from app.worker.graph.graph import COMPILED_GRAPH, respond
from app.worker.graph.state import GraphContext, GraphState
from app.worker.pubsub import publish_error

settings = get_settings()
logger = get_logger(__name__)


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


async def _upsert_user_message(
    session, tenant_id: str, conversation_id: uuid.UUID, client_message_id: str, content: str, trace_id: Optional[str]
) -> str:
    """插入用户消息，返回三种结果之一：

    - "inserted"：全新消息，正常往下走生成回复
    - "retry"：(tenant_id, message_id) 之前插过，但 status 还是 received——说明上一次处理到一半
      （比如调完 LLM、还没来得及写回复）worker 就崩了，这次要重新走一遍生成回复的流程，
      不能直接当"已处理"跳过，否则用户永远收不到回复
    - "done"：之前已经完整回复过（status=replied），是真正的重复投递，跳过

    只用 ON CONFLICT DO NOTHING 判断"插没插成功"是不够的：插入失败只能说明这条消息之前来过，
    不能说明有没有回复完，所以冲突之后还要多查一次 status。
    """
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
            status=MessageStatus.received,
        )
        .on_conflict_do_nothing(constraint="uq_messages_tenant_message_id")
        .returning(Message.id)
    )
    result = await session.execute(stmt)
    if result.first() is not None:
        return "inserted"

    existing = await session.execute(
        select(Message.status).where(Message.tenant_id == tenant_id, Message.message_id == client_message_id)
    )
    status = existing.scalar_one()
    return "done" if status == MessageStatus.replied else "retry"


async def _load_recent_messages(
    session, tenant_id: str, conversation_id: uuid.UUID, limit: int, exclude_message_id: Optional[str] = None
) -> List[dict]:
    """历史消息不包含当前这条——LangGraph 的 state 把"历史"和"当前用户消息"分开放
    （PHASE2.md 2.7 第 1 点），排除掉当前 message_id 避免这条消息在 LLM 的上下文里出现两次。
    """
    conditions = [Message.tenant_id == tenant_id, Message.conversation_id == conversation_id]
    if exclude_message_id is not None:
        conditions.append(Message.message_id != exclude_message_id)
    stmt = select(Message.role, Message.content).where(*conditions).order_by(Message.created_at.desc()).limit(limit)
    result = await session.execute(stmt)
    rows = list(result.all())
    rows.reverse()  # 数据库按时间倒序取出的，喂给 LLM 前要反转成"旧的在前，新的在后"
    return [{"role": row.role.value, "content": row.content} for row in rows]


async def _insert_assistant_message(
    session,
    tenant_id: str,
    conversation_id: uuid.UUID,
    content: str,
    trace_id: Optional[str],
    intent: Optional[str],
    meta: dict[str, Any],
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
            intent=intent,
            meta=meta,
        )
    )


async def _mark_user_message_replied(session, tenant_id: str, client_message_id: str) -> None:
    # 回复真正写完之后再打这个标记，这样"标记为 replied"和"消息处理完成"永远是同一时刻，
    # 中途崩溃的话这行就不会被执行，下次重投递会走 "retry" 分支重新生成回复
    await session.execute(
        update(Message)
        .where(Message.tenant_id == tenant_id, Message.message_id == client_message_id)
        .values(status=MessageStatus.replied)
    )


def _metric_result(route_source: Optional[str]) -> str:
    """算错误率用的粗粒度结果：route_source=rule_fallback 说明 LLM 调用本身失败了（不管关键词
    兜底最后判没判出意图），跟阶段一"llm_degraded"是同一件事；rule/llm 都是 LLM 链路正常，算 ok。
    LLM 返回非法输出（invalid_output）不算这里的"降级"——LLM 本身是通的，系统只是正确地没有
    执行未经校验的输出，请求依然端到端处理完了，所以也算 ok。
    """
    return "llm_degraded" if route_source == "rule_fallback" else "ok"


async def process_inbound_message(
    *,
    tenant_id: str,
    user_id: str,
    conversation_id_raw: str,
    client_message_id: str,
    content: str,
    trace_id: Optional[str],
) -> tuple[str, Optional[str]]:
    """返回 (result, intent) 打指标用：result 是阶段一定下来的粗粒度取值（ok/forbidden/duplicate/
    llm_degraded），给错误率统计用；intent 是跑完图之后的具体意图（forbidden/duplicate 场景还没
    跑图，intent 是 None）。

    所有"预期内"的情况（越权、去重命中、LLM 失败）都在这里或 LangGraph 编排（app/worker/graph）
    内部处理完并正常返回，不往外抛异常；真正往外抛的异常（数据库挂了、conversation_id 格式非法
    等）由上层（consumer）判定为不可预期异常，reject 进死信，这里不用关心队列层面的 ack/reject。
    """
    async with AsyncSessionLocal() as session:
        try:
            conversation_id = await _resolve_conversation(session, tenant_id, user_id, conversation_id_raw)
        except ConversationForbidden:
            await session.rollback()
            await publish_error(tenant_id, user_id, client_message_id, "forbidden", "该会话不属于当前用户")
            logger.warning("越权访问会话", conversation_id=conversation_id_raw)
            return "forbidden", None

        outcome = await _upsert_user_message(
            session, tenant_id, conversation_id, client_message_id, content, trace_id
        )
        await session.commit()
        if outcome == "done":
            logger.info("消息已完整回复过，跳过", message_id=client_message_id)
            return "duplicate", None
        if outcome == "retry":
            logger.warning(
                "消息之前处理到一半就中断了（用户消息已入库但未回复），重新生成回复",
                message_id=client_message_id,
            )

        history = await _load_recent_messages(
            session,
            tenant_id,
            conversation_id,
            settings.conversation_history_limit,
            exclude_message_id=client_message_id,
        )
        await session.commit()

    initial_state: GraphState = {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "conversation_id": str(conversation_id),
        "message_id": client_message_id,
        "trace_id": trace_id,
        "content": content,
        "history": history,
    }

    # classify 节点要查 users/pending_actions 表，还要调一次非流式 LLM 判断意图，这段会占用一个
    # 数据库连接；真正可能耗时更久的流式生成挪到 respond()，respond() 在这个 session 关闭之后才跑，
    # 不占数据库连接
    async with AsyncSessionLocal() as graph_session:
        final_state: GraphState = await COMPILED_GRAPH.ainvoke(
            initial_state, context=GraphContext(session=graph_session)
        )

    reply_text, meta = await respond(tenant_id, user_id, client_message_id, final_state)

    async with AsyncSessionLocal() as session:
        await _insert_assistant_message(
            session, tenant_id, conversation_id, reply_text, trace_id, meta.get("intent"), meta
        )
        # respond() 跑完就代表已经给用户答复过了（不管走的是哪条分支），标记 replied
        await _mark_user_message_replied(session, tenant_id, client_message_id)
        await session.commit()

    return _metric_result(meta.get("route_source")), meta.get("intent")
