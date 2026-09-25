"""LLM 用量记录 + 每日 token 预算（PHASE3.md 第 6 步，设计决定 13）。

预算键按机构时区算日期（`llm:budget:{tenant_id}:{YYYY-MM-DD}`），过期时间给 2 天——够跨零点
那一刻还能被前一天的查询读到，又不会让 Redis 里堆积太久的旧键。调用前检查、调用后累加：
检查和累加不是一个原子操作，两次调用之间理论上有一点点竞态窗口，单次超出一点点是设计上
接受的代价（PHASE3.md 关键设计决定 13 原文就是这么写的）。Redis 不可用时预算检查直接放行
（跟限流、去重的降级策略一致，见设计决定 9），不能让 Redis 挂了导致所有 LLM 调用被误判超支。
"""
import datetime as dt
import uuid
from typing import Optional
from zoneinfo import ZoneInfo

from prometheus_client import Counter
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.config import get_settings
from app.common.logging import get_logger
from app.common.models import LlmUsage, Tenant
from app.common.redis import note_redis_result, redis_client

settings = get_settings()
logger = get_logger(__name__)

_BUDGET_TTL_SECONDS = 2 * 24 * 3600

# 只按机构和方向（输入/输出）分，不按 user_id、也不按 purpose——设计决定 14：用户数量多，
# 按用户分会让指标基数失控；purpose 细分留给 llm_usage 表按 SQL 查，Prometheus 只看总量趋势
llm_tokens_total = Counter("worker_llm_tokens_total", "LLM token 用量，按机构和方向分", ["tenant_id", "direction"])


def _today_str(tenant_timezone: str) -> str:
    return dt.datetime.now(ZoneInfo(tenant_timezone)).strftime("%Y-%m-%d")


def _budget_key(tenant_id: str, tenant_timezone: str) -> str:
    return f"llm:budget:{tenant_id}:{_today_str(tenant_timezone)}"


async def get_daily_budget(session: AsyncSession, tenant_id: str) -> Optional[int]:
    """机构自己设了 daily_token_budget 就用那个值（哪怕是 0，表示直接禁用 LLM），
    没设置（None）才落回 .env 的默认值。"""
    tenant = await session.get(Tenant, tenant_id)
    if tenant is not None and tenant.daily_token_budget is not None:
        return tenant.daily_token_budget
    return settings.default_daily_token_budget


async def is_budget_exceeded(tenant_id: str, tenant_timezone: str, budget: Optional[int]) -> bool:
    if budget is None:
        return False
    try:
        used = await redis_client.get(_budget_key(tenant_id, tenant_timezone))
        note_redis_result(True)
    except RedisError as exc:
        note_redis_result(False)
        logger.warning("token 预算检查调用 Redis 失败，放行", tenant_id=tenant_id, error=str(exc))
        return False
    # 今天还没调用过 LLM 时 Redis 里没有这个键，used 是 None，按 0 算——budget=0（机构被
    # 直接停用 LLM）时这里必须一开始就判定超限，不能因为"今天还没用过"就放第一次调用过去
    return int(used or 0) >= budget


async def add_tokens_used(tenant_id: str, tenant_timezone: str, total_tokens: int) -> None:
    if total_tokens <= 0:
        return
    key = _budget_key(tenant_id, tenant_timezone)
    try:
        async with redis_client.pipeline(transaction=False) as pipe:
            pipe.incrby(key, total_tokens)
            pipe.expire(key, _BUDGET_TTL_SECONDS)
            await pipe.execute()
        note_redis_result(True)
    except RedisError as exc:
        note_redis_result(False)
        logger.warning("token 预算累加调用 Redis 失败，本次用量不计入预算", tenant_id=tenant_id, error=str(exc))


async def record_llm_usage(
    session: AsyncSession,
    *,
    tenant_id: str,
    conversation_id: Optional[str],
    trace_id: Optional[str],
    purpose: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    estimated: bool,
) -> None:
    """写 llm_usage 表。失败只打日志，不能因为记账失败就影响已经生成好的回复（PHASE3.md 原文：
    "写入失败不影响回复，只打日志"）。"""
    try:
        session.add(
            LlmUsage(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                conversation_id=uuid.UUID(conversation_id) if conversation_id else None,
                trace_id=trace_id,
                purpose=purpose,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                estimated=estimated,
            )
        )
        await session.commit()
    except Exception:
        await session.rollback()
        logger.warning("记录 token 用量失败，不影响回复", purpose=purpose, exc_info=True)
        return

    llm_tokens_total.labels(tenant_id=tenant_id, direction="prompt").inc(prompt_tokens)
    llm_tokens_total.labels(tenant_id=tenant_id, direction="completion").inc(completion_tokens)
