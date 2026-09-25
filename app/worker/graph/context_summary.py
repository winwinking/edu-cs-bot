"""上下文：最近 10 条原样保留，更早的压成摘要（PHASE3.md 第 3 步，关键设计决定 7）。

摘要在回复发完之后才生成（不拖慢首 token），存进 conversation_summaries 表；后续对话把摘要
放进 user 消息的 <历史摘要> 块里——不能放进 system prompt，因为摘要里包含用户说过的话，
这是用户输入，硬性规则不允许拼进 system prompt（跟 <资料>/<提醒列表> 是同一个道理）。

生成失败（LLM 调用异常/空结果）就保留旧摘要、打日志，不在这里重试——下一条消息处理完之后
会再检查一次，待摘要的消息只会越攒越多，不会漏成"永远生成不了"。
"""
import uuid
from typing import Optional

from openai import APIConnectionError, APIError, APITimeoutError
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.config import get_settings
from app.common.llm_client import LLM_MODEL, chat_completion, estimate_tokens, extract_usage_from_response
from app.common.llm_usage import add_tokens_used, get_daily_budget, is_budget_exceeded, record_llm_usage
from app.common.logging import get_logger
from app.common.masking import mask_text
from app.common.models import ConversationSummary, Message

logger = get_logger(__name__)
settings = get_settings()

# 这个标记词放在 system prompt 里，不是用户内容——mock-llm 靠它识别"这是一次摘要生成请求"，
# 不按用户/对话内容里的关键词判断，避免像转人工摘要标记那样被真实对话内容意外撞上
_SUMMARY_SYSTEM_MARKER = (
    "历史摘要生成：请把下面提供的旧摘要（可能为空）和新增对话合并成一段新的中文摘要，"
    "客观记录用户提过的问题、诉求和关键信息，不要加评价，不要编造没有出现过的内容，"
    "控制在 200 字以内。"
)


def format_summary_block(summary: str) -> str:
    return f"<历史摘要>\n{summary}\n</历史摘要>"


def append_summary_block(content: str, history_summary: Optional[str]) -> str:
    """classify/chitchat/knowledge 组装 user 消息时统一调用这个：没有摘要就原样返回，
    避免每个调用点各自判断一遍"有没有摘要要不要拼"。"""
    if not history_summary:
        return content
    return f"{content}\n\n{format_summary_block(history_summary)}"


def should_regenerate_summary(uncovered_count: int) -> bool:
    """"最近 10 条之前、还没被摘要覆盖"的消息超过这个阈值才重新生成——跟"最近几条原样保留"
    共用 CONVERSATION_HISTORY_LIMIT，没必要为摘要单独加一个阈值配置。"""
    return uncovered_count > settings.conversation_history_limit


def build_summary_messages(old_summary: Optional[str], transcript_messages: list[dict]) -> list[dict[str, str]]:
    """摘要生成请求本身的消息列表（纯函数，方便单测）：标记词是代码写的固定指令，放 system；
    旧摘要和新增对话原文包含用户说过的话，放 user。"""
    transcript = "\n".join(
        f"{'用户' if m['role'] == 'user' else '客服'}：{m['content']}" for m in transcript_messages
    )
    old_part = f"旧摘要：{old_summary}\n\n" if old_summary else "旧摘要：（无）\n\n"
    return [
        {"role": "system", "content": _SUMMARY_SYSTEM_MARKER},
        {"role": "user", "content": f"{old_part}新增对话：\n{transcript}"},
    ]


async def _generate_summary_text(
    session: AsyncSession,
    tenant_id: str,
    tenant_timezone: str,
    conversation_id: uuid.UUID,
    old_summary: Optional[str],
    transcript_messages: list[dict],
) -> Optional[str]:
    # 机构今日 token 预算用完就跳过这一轮生成（PHASE3.md 第 6 步，设计决定 13："摘要跳过"）：
    # 旧摘要留着不动，待覆盖的消息数只会越攒越多，下一条消息处理完之后会再检查一次，不会漏
    budget = await get_daily_budget(session, tenant_id)
    if await is_budget_exceeded(tenant_id, tenant_timezone, budget):
        logger.info("机构今日 token 预算已用完，跳过本轮历史摘要生成", tenant_id=tenant_id)
        return None

    messages = build_summary_messages(old_summary, transcript_messages)
    try:
        response = await chat_completion(messages=messages)
        text = (response.choices[0].message.content or "").strip()
    except (APIError, APITimeoutError, APIConnectionError) as exc:
        logger.warning("生成历史摘要失败，保留旧摘要，下次再试", error=str(exc))
        return None
    if not text:
        return None

    usage = extract_usage_from_response(response)
    estimated = usage is None
    prompt_tokens, completion_tokens = usage or estimate_tokens(messages, text)
    await add_tokens_used(tenant_id, tenant_timezone, prompt_tokens + completion_tokens)
    await record_llm_usage(
        session,
        tenant_id=tenant_id,
        conversation_id=str(conversation_id),
        trace_id=None,
        purpose="summary",
        model=LLM_MODEL,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        estimated=estimated,
    )
    # 摘要要长期存库、还会被拼进以后每一轮的 LLM 请求里，必须先脱敏（硬性规则）
    return mask_text(text)


async def load_history_summary(
    session: AsyncSession, tenant_id: str, conversation_id: uuid.UUID
) -> Optional[str]:
    result = await session.execute(
        select(ConversationSummary.summary).where(
            ConversationSummary.tenant_id == tenant_id, ConversationSummary.conversation_id == conversation_id
        )
    )
    row = result.first()
    return row[0] if row else None


async def maybe_update_summary(
    session: AsyncSession, tenant_id: str, conversation_id: uuid.UUID, tenant_timezone: str
) -> None:
    """回复发完之后调用。找出"最近 limit 条之前、还没被摘要覆盖"的消息，够阈值就重新生成，
    不够或者生成失败都保持现状。"""
    limit = settings.conversation_history_limit

    # 最近 limit 条（原样保留，永远不会被摘要覆盖）里最早一条的时间，划出"多早算旧"的边界
    boundary_result = await session.execute(
        select(Message.created_at)
        .where(Message.tenant_id == tenant_id, Message.conversation_id == conversation_id)
        .order_by(Message.created_at.desc())
        .offset(limit - 1)
        .limit(1)
    )
    boundary_row = boundary_result.first()
    if boundary_row is None:
        return  # 总共还没攒够 limit 条消息，压根没有"更早的部分"需要摘要
    boundary_created_at = boundary_row[0]

    existing_result = await session.execute(
        select(ConversationSummary).where(
            ConversationSummary.tenant_id == tenant_id, ConversationSummary.conversation_id == conversation_id
        )
    )
    summary_row = existing_result.scalar_one_or_none()
    covered_until = summary_row.covered_until if summary_row else None

    conditions = [
        Message.tenant_id == tenant_id,
        Message.conversation_id == conversation_id,
        Message.created_at < boundary_created_at,
    ]
    if covered_until is not None:
        conditions.append(Message.created_at > covered_until)

    pending_result = await session.execute(
        select(Message.role, Message.content, Message.created_at)
        .where(*conditions)
        .order_by(Message.created_at.asc())
    )
    pending_rows = pending_result.all()

    if not should_regenerate_summary(len(pending_rows)):
        return

    transcript_messages = [{"role": row.role.value, "content": row.content} for row in pending_rows]
    new_summary = await _generate_summary_text(
        session,
        tenant_id,
        tenant_timezone,
        conversation_id,
        summary_row.summary if summary_row else None,
        transcript_messages,
    )
    if new_summary is None:
        return  # 保留旧摘要，不在这里重试

    new_covered_until = pending_rows[-1].created_at
    stmt = (
        pg_insert(ConversationSummary)
        .values(
            conversation_id=conversation_id,
            tenant_id=tenant_id,
            summary=new_summary,
            covered_until=new_covered_until,
        )
        .on_conflict_do_update(
            index_elements=["conversation_id"],
            set_={"summary": new_summary, "covered_until": new_covered_until, "updated_at": func.now()},
        )
    )
    await session.execute(stmt)
    await session.commit()
    logger.info(
        "历史摘要已更新",
        conversation_id=str(conversation_id),
        covered_messages=len(pending_rows),
    )
