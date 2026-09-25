"""每秒扫一次到期提醒（PHASE3.md 第 2 步）。

一个事务里用 FOR UPDATE SKIP LOCKED 取最多 N 条到期的 active 提醒（开多个 scheduler 实例，
SKIP LOCKED 保证不会重复取到同一条）；对每一条先推 Redis，推送成功了才更新 next_trigger_at
（或者标成 done），整批处理完最后一次性提交（关键设计决定 4：先推送再提交）。

这意味着：如果进程在推完第 30 条、还没提交第 100 条这批之前崩溃，前 30 条也会因为事务回滚
被重新判定成"还没处理"，重启后会再推一次——这个代价比"先提交再推送、推送失败提醒就永久丢了"
小得多，接受，记为已知问题。

单条提醒推送失败（Redis 暂时不可用）时，不改这一条的任何字段，让它留在这次事务里"什么都
没发生"的状态：其余成功的提醒该更新还是照常更新，随事务一起提交；失败的这条下一秒会被同一个
查询重新选中再试一次，Redis 恢复后自动补推，提醒不会丢。
"""
import asyncio
import uuid
from datetime import datetime, timezone

from redis.exceptions import RedisError
from sqlalchemy import func, select

from app.common.config import get_settings
from app.common.db import AsyncSessionLocal
from app.common.logging import bind_trace_context, clear_trace_context, get_logger
from app.common.models import Message, MessageRole, Reminder, ReminderStatus
from app.common.reminder_rules import advance_after_trigger, resolve_timezone
from app.scheduler.metrics import reminder_push_delay_seconds, reminder_push_total
from app.scheduler.pubsub import publish_reminder_push

settings = get_settings()
logger = get_logger(__name__)


def _format_relative_date(event_local: datetime, now_local: datetime) -> str:
    diff_days = (event_local.date() - now_local.date()).days
    if diff_days == 0:
        return "今天"
    if diff_days == 1:
        return "明天"
    return f"{event_local.month}月{event_local.day}日"


def build_push_text(reminder: Reminder, now_utc: datetime) -> str:
    tz = resolve_timezone(reminder.timezone)
    event_local = reminder.event_at.astimezone(tz)
    now_local = now_utc.astimezone(tz)
    date_label = _format_relative_date(event_local, now_local)
    return f"提醒：{date_label} {event_local:%H:%M} {reminder.title}，还有 {reminder.advance_minutes} 分钟开始。"


async def _process_due_reminders() -> int:
    async with AsyncSessionLocal() as session:
        # 用数据库自己的时钟判断"到期没有"，跟 PendingAction 的 expires_at 判断是同一个
        # 理由：应用服务器和数据库服务器的时钟可能有细微偏差，"现在几点"以数据库为准
        now = (await session.execute(select(func.now()))).scalar_one()

        stmt = (
            select(Reminder)
            .where(Reminder.status == ReminderStatus.active, Reminder.next_trigger_at <= now)
            .order_by(Reminder.next_trigger_at)
            .limit(settings.scheduler_batch_size)
            .with_for_update(skip_locked=True)
        )
        due = list((await session.execute(stmt)).scalars().all())
        if not due:
            return 0

        processed = 0
        for item in due:
            # 人审发现（排查摘要日志时顺带发现，见 AGENT_LOG）：scheduler 之前从没调用过
            # bind_trace_context，所有日志都没有 tenant_id/trace_id，不满足 NFR-4、也没法按
            # 提醒/机构追踪。每条到期提醒现生成一个 trace_id，处理完立刻清空，不串到下一条——
            # 一条提醒的处理是这个循环里最小的、有意义的追踪单元，不用整批共用一个 trace_id
            # （那样多条提醒的日志会分不清是哪一条）。
            trace_id = uuid.uuid4().hex
            bind_trace_context(trace_id=trace_id, tenant_id=item.tenant_id)
            try:
                text = build_push_text(item, now)
                try:
                    await publish_reminder_push(item.tenant_id, item.user_id, str(item.id), item.title, text)
                except RedisError as exc:
                    logger.warning(
                        "提醒推送失败，这一条这次不更新触发时间，下一秒再试",
                        user_id=item.user_id,
                        reminder_id=str(item.id),
                        error=str(exc),
                    )
                    reminder_push_total.labels(result="error").inc()
                    continue

                logger.info("提醒已推送", user_id=item.user_id, reminder_id=str(item.id))
                reminder_push_total.labels(result="ok").inc()
                # 这里读 next_trigger_at 还是这一条本来到期的时间——下面 advance_after_trigger()
                # 才会把它改成下一次触发时间，晚一步读就量不出真实的推送延迟了
                reminder_push_delay_seconds.observe((now - item.next_trigger_at).total_seconds())

                session.add(
                    Message(
                        id=uuid.uuid4(),
                        tenant_id=item.tenant_id,
                        conversation_id=item.conversation_id,
                        message_id=f"reminder-{uuid.uuid4()}",
                        role=MessageRole.assistant,
                        content=text,
                        intent="reminder_push",
                        meta={"reminder_id": str(item.id)},
                    )
                )

                advance = advance_after_trigger(
                    item.event_at, item.timezone, item.repeat.value, item.advance_minutes, now
                )
                if advance is None:
                    item.status = ReminderStatus.done
                else:
                    item.event_at, item.next_trigger_at = advance
                processed += 1
            finally:
                clear_trace_context()

        await session.commit()
        return processed


async def run_scheduler_loop() -> None:
    logger.info("scheduler 开始扫描到期提醒", interval_seconds=settings.scheduler_interval_seconds)
    while True:
        try:
            processed = await _process_due_reminders()
            if processed:
                logger.info("本轮推送了到期提醒", count=processed)
        except asyncio.CancelledError:
            raise
        except Exception:
            # 不能因为这一轮出了意外（比如数据库短暂连不上）就让整个 scheduler 进程退出——
            # 下一轮还会再试，这跟 worker consumer 对不可预期异常的处理思路一致：记日志，不崩溃
            logger.error("扫描到期提醒时出现异常", exc_info=True)
        await asyncio.sleep(settings.scheduler_interval_seconds)
