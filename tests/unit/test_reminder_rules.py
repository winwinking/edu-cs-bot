"""覆盖 PHASE3.md 第 1 步验证清单：每天/每周/工作日、提前提醒、跨天、事件已过、立刻提醒、
错过多次只补一次、非 Asia/Shanghai 时区。全部是纯函数测试，不连数据库、不起容器。
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.common.reminder_rules import (
    ReminderRuleError,
    advance_after_trigger,
    compute_creation_trigger,
    compute_next_occurrence,
    parse_local_datetime,
    resolve_timezone,
    validate_advance_minutes,
)


def _shanghai(y, m, d, h, minute) -> datetime:
    return datetime(y, m, d, h, minute, tzinfo=ZoneInfo("Asia/Shanghai"))


# ---------- 重复规则：算下一次 event_at ----------


def test_daily_repeat_advances_one_local_day():
    event_at = _shanghai(2026, 9, 25, 9, 0).astimezone(timezone.utc)
    next_event = compute_next_occurrence(event_at, "Asia/Shanghai", "daily")
    assert next_event.astimezone(ZoneInfo("Asia/Shanghai")) == _shanghai(2026, 9, 26, 9, 0)


def test_weekly_repeat_advances_seven_local_days():
    event_at = _shanghai(2026, 9, 25, 9, 0).astimezone(timezone.utc)
    next_event = compute_next_occurrence(event_at, "Asia/Shanghai", "weekly")
    assert next_event.astimezone(ZoneInfo("Asia/Shanghai")) == _shanghai(2026, 10, 2, 9, 0)


def test_workdays_repeat_from_friday_jumps_to_monday():
    # 2024-01-05 是周五（2024-01-01 是周一，这是历史事实，不是猜的）
    friday = _shanghai(2024, 1, 5, 9, 0)
    assert friday.weekday() == 4  # 断言前提本身成立：这天确实是周五
    event_at = friday.astimezone(timezone.utc)
    next_event = compute_next_occurrence(event_at, "Asia/Shanghai", "workdays")
    next_local = next_event.astimezone(ZoneInfo("Asia/Shanghai"))
    assert next_local == _shanghai(2024, 1, 8, 9, 0)  # 跳过周六周日，落在下周一
    assert next_local.weekday() == 0


# ---------- 创建时的 next_trigger_at ----------


def test_advance_thirty_minutes_before_event():
    now = datetime(2026, 9, 25, 6, 0, tzinfo=timezone.utc)
    event_at = now + timedelta(hours=2)
    trigger = compute_creation_trigger(event_at, advance_minutes=30, now_utc=now)
    assert trigger == event_at - timedelta(minutes=30)


def test_event_already_past_is_rejected():
    now = datetime(2026, 9, 25, 6, 0, tzinfo=timezone.utc)
    event_at = now - timedelta(hours=1)
    with pytest.raises(ReminderRuleError) as exc_info:
        compute_creation_trigger(event_at, advance_minutes=30, now_utc=now)
    assert exc_info.value.reason == "event_in_past"


def test_less_than_advance_window_away_triggers_immediately():
    # 事件还有 10 分钟就到，但提前量是 30 分钟——"事件时间 - 提前量"已经是过去时刻，
    # 应该立刻提醒（next_trigger_at = now），而不是拒绝创建
    now = datetime(2026, 9, 25, 6, 0, tzinfo=timezone.utc)
    event_at = now + timedelta(minutes=10)
    trigger = compute_creation_trigger(event_at, advance_minutes=30, now_utc=now)
    assert trigger == now


def test_crossing_midnight_still_triggers_immediately_when_advance_point_already_passed():
    # 今晚 23:50（本地）设"明天 00:10"的提醒（今天这个点已经过了，所以事件时间落在明天）：
    # 事件时间 - 30 分钟提前量 = 今天 23:40，比"现在"（23:50）还早，应该立刻提醒，
    # 这一步的日期计算横跨了一次午夜
    now = _shanghai(2026, 9, 25, 23, 50).astimezone(timezone.utc)
    event_at = _shanghai(2026, 9, 26, 0, 10).astimezone(timezone.utc)
    trigger = compute_creation_trigger(event_at, advance_minutes=30, now_utc=now)
    assert trigger == now


# ---------- 错过多次的重复提醒 ----------


def test_missed_multiple_daily_cycles_jumps_to_next_future_occurrence_only():
    # scheduler 停了 5 天，重复提醒从 5 天前就该触发了；恢复后不应该把 5 次都补上，
    # 只应该跳到下一个将来的时间
    now = datetime(2026, 9, 25, 6, 0, tzinfo=timezone.utc)
    event_at = now - timedelta(days=5)
    result = advance_after_trigger(event_at, "Asia/Shanghai", "daily", advance_minutes=30, now_utc=now)
    assert result is not None
    next_event, next_trigger = result
    assert next_trigger > now
    # 只补到下一个将来的时间，不会因为错过太多次而被推到很远的未来
    assert next_trigger - now < timedelta(days=1, minutes=1)
    assert (next_event - event_at) % timedelta(days=1) == timedelta(0)


def test_repeat_none_returns_none_for_scheduler_to_mark_done():
    now = datetime(2026, 9, 25, 6, 0, tzinfo=timezone.utc)
    event_at = now - timedelta(minutes=1)
    assert advance_after_trigger(event_at, "Asia/Shanghai", "none", advance_minutes=30, now_utc=now) is None


# ---------- 非 Asia/Shanghai 时区（覆盖夏令时，Asia/Shanghai 没有夏令时看不出这条逻辑的价值）----------


def test_non_shanghai_timezone_daily_repeat_crosses_dst_spring_forward():
    # 2024-03-10 美国东部进入夏令时（凌晨 2 点跳到 3 点）。本地时间每天都固定是 09:00，
    # 但换算成 UTC 之后，3 月 9 日到 3 月 10 日之间只隔了 23 小时，不是 24 小时——
    # 如果直接在 UTC 上加 24 小时会把本地时刻错误地漂移一小时，转到本地时区再加一天不会
    tz = ZoneInfo("America/New_York")
    event_at = datetime(2024, 3, 9, 9, 0, tzinfo=tz).astimezone(timezone.utc)
    next_event = compute_next_occurrence(event_at, "America/New_York", "daily")

    next_local = next_event.astimezone(tz)
    assert next_local == datetime(2024, 3, 10, 9, 0, tzinfo=tz)
    assert (next_event - event_at) == timedelta(hours=23)  # UTC 间隔只有 23 小时，印证跨过了夏令时


# ---------- 时区/格式/范围校验 ----------


def test_resolve_invalid_timezone_is_rejected():
    with pytest.raises(ReminderRuleError) as exc_info:
        resolve_timezone("Mars/Olympus_Mons")
    assert exc_info.value.reason == "invalid_timezone"


def test_validate_advance_minutes_accepts_boundary_values():
    validate_advance_minutes(0)
    validate_advance_minutes(1440)


@pytest.mark.parametrize("value", [-1, 1441])
def test_validate_advance_minutes_rejects_out_of_range(value):
    with pytest.raises(ReminderRuleError) as exc_info:
        validate_advance_minutes(value)
    assert exc_info.value.reason == "invalid_advance_minutes"


def test_parse_local_datetime_accepts_space_and_t_separator():
    tz = ZoneInfo("Asia/Shanghai")
    assert parse_local_datetime("2026-09-26 09:00", tz) == datetime(2026, 9, 26, 9, 0, tzinfo=tz)
    assert parse_local_datetime("2026-09-26T09:00", tz) == datetime(2026, 9, 26, 9, 0, tzinfo=tz)


def test_parse_local_datetime_rejects_calendar_invalid_date():
    with pytest.raises(ReminderRuleError) as exc_info:
        parse_local_datetime("2026-02-30 09:00", ZoneInfo("Asia/Shanghai"))
    assert exc_info.value.reason == "invalid_time_format"
