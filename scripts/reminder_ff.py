"""快进脚本：把指定用户最新一条生效中的提醒的 next_trigger_at 改成"现在 + 3 秒"，
不用真的等到点就能验证 scheduler 推送链路（PHASE3.md 第 2 步）。

这是一个直接改数据库、绕过正常业务规则的调试工具，只能在 tools 容器里手动跑；
APP_ENV=production 时直接拒绝执行，避免它被带进生产环境的可执行路径。

用法：python scripts/reminder_ff.py --tenant t_a --user u_a_1001
"""
import argparse
import asyncio
from datetime import timedelta

from sqlalchemy import func, select, update

from app.common.config import get_settings
from app.common.db import AsyncSessionLocal
from app.common.models import Reminder, ReminderStatus

settings = get_settings()

FAST_FORWARD_SECONDS = 3


async def main(tenant: str, user: str) -> None:
    if settings.app_env == "production":
        raise SystemExit("拒绝执行：APP_ENV=production 时不允许快进提醒")

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Reminder.id, Reminder.title)
            .where(
                Reminder.tenant_id == tenant,
                Reminder.user_id == user,
                Reminder.status == ReminderStatus.active,
            )
            .order_by(Reminder.created_at.desc())
            .limit(1)
        )
        row = result.first()
        if row is None:
            raise SystemExit(f"没找到生效中的提醒：tenant={tenant} user={user}")
        reminder_id, title = row

        # 用数据库自己的时钟 + 3 秒，跟 scheduler 判断"到期没有"用的是同一个时钟源，
        # 不会因为应用容器和数据库容器的系统时钟有细微偏差而算错
        stmt = (
            update(Reminder)
            .where(Reminder.id == reminder_id)
            .values(next_trigger_at=func.now() + timedelta(seconds=FAST_FORWARD_SECONDS))
            .returning(Reminder.next_trigger_at)
        )
        result = await session.execute(stmt)
        new_trigger_at = result.scalar_one()
        await session.commit()

    print(f"已快进：提醒《{title}》（{reminder_id}）next_trigger_at -> {new_trigger_at.isoformat()}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="把指定用户最新一条生效提醒快进到 3 秒后触发")
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--user", required=True)
    args = parser.parse_args()
    asyncio.run(main(args.tenant, args.user))
