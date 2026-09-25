"""提醒的纯规则计算（PHASE3.md 第 1 步）：只做时间/规则计算，不连数据库、不调网络，方便单元
测试覆盖每种重复规则和边界情况，不用起真实容器。

设计要点（对应 PHASE3.md「关键设计决定」1、5、6）：
- 数据库里所有时间都是 UTC；这里的入参/出参统一是 UTC 的 aware datetime，只有中间要算
  "下一次是哪一天"时才临时转换到用户时区算日期，算完立刻转回 UTC——这样"每天"这类重复规则
  才不会因为 UTC 和本地时区的日期分界不一样而错位（尤其是有夏令时的时区，直接在 UTC 上加
  24 小时会漏掉/多算一小时，见 test_reminder_rules.py 的非 Asia/Shanghai 时区用例）。
- 创建时：事件时间已经过去 -> 拒绝创建；"事件时间 - 提前量"已经过去但事件本身还没到 ->
  next_trigger_at 设为现在，立刻提醒。
- 重复提醒错过了好几次（比如 scheduler 停了两天）时，直接跳到下一个将来的时间，不用把错过的
  每一次都补一遍——advance_after_trigger 用 while 循环持续推进，直到算出的下一次触发时间
  在未来为止。
"""
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

RepeatRule = Literal["none", "daily", "weekly", "workdays"]

MIN_ADVANCE_MINUTES = 0
MAX_ADVANCE_MINUTES = 1440

# LLM 输出的本地时间字符串只接受这两种格式，两种都是"日期 空格或T 时:分"，不支持带秒
_DATETIME_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M")


class ReminderRuleError(Exception):
    """提醒的时间/规则不合法：事件已过、时区不认识、提前量超范围、时间格式解析不了。

    reason 是机器可读的分类（给 worker 节点决定回哪句话术用），detail 是中文说明（给日志/
    排障用，不直接回给用户——用户看到的是节点按 reason 挑的固定话术，不是这里的 detail 原文）。
    """

    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def resolve_timezone(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ReminderRuleError("invalid_timezone", f"不支持的时区：{timezone_name}") from exc


def validate_advance_minutes(advance_minutes: int) -> None:
    if not (MIN_ADVANCE_MINUTES <= advance_minutes <= MAX_ADVANCE_MINUTES):
        raise ReminderRuleError(
            "invalid_advance_minutes",
            f"提前提醒必须在 {MIN_ADVANCE_MINUTES} 到 {MAX_ADVANCE_MINUTES} 分钟之间",
        )


def parse_local_datetime(text: str, tz: ZoneInfo) -> datetime:
    """把 LLM 给出的本地时间字符串解析成带时区的 aware datetime。

    格式本身的粗校验（是不是"数字-数字-数字 数字:数字"这个形状）已经在 Pydantic 层
    （app.common.tools.ManageReminderArgs）做过一次；这里再做一次真正的日历合法性校验
    （比如 2 月 30 日这种格式对但日期不存在的输入），两层校验各管各的，不重复实现。
    """
    stripped = text.strip()
    for fmt in _DATETIME_FORMATS:
        try:
            naive = datetime.strptime(stripped, fmt)
        except ValueError:
            continue
        return naive.replace(tzinfo=tz)
    raise ReminderRuleError("invalid_time_format", f"无法识别的时间格式：{text}")


def compute_creation_trigger(event_at_utc: datetime, advance_minutes: int, now_utc: datetime) -> datetime:
    """创建时算 next_trigger_at；事件本身已经过去就直接拒绝创建（PHASE3.md 第 1 步的创建规则）。"""
    if event_at_utc <= now_utc:
        raise ReminderRuleError("event_in_past", "事件时间已经过去，不能创建这条提醒")

    next_trigger = event_at_utc - timedelta(minutes=advance_minutes)
    if next_trigger <= now_utc:
        # "事件时间 - 提前量"已经过了，但事件本身还没到——现在立刻提醒，不能因为算出来是
        # 过去的时间点就跟着拒绝创建（这跟上面"事件本身已过"是两种不同的情况）
        return now_utc
    return next_trigger


def _advance_local_date(local_dt: datetime, repeat: RepeatRule) -> datetime:
    if repeat == "daily":
        return local_dt + timedelta(days=1)
    if repeat == "weekly":
        return local_dt + timedelta(days=7)
    if repeat == "workdays":
        nxt = local_dt + timedelta(days=1)
        while nxt.weekday() >= 5:  # 5=周六，6=周日；跳过周末，直接落到下一个工作日
            nxt += timedelta(days=1)
        return nxt
    raise ValueError(f"这个规则不需要算下一次触发：{repeat}")


def compute_next_occurrence(event_at_utc: datetime, timezone_name: str, repeat: RepeatRule) -> datetime:
    """算重复提醒触发一次之后，下一次的 event_at（UTC）。

    先转换到用户时区算"下一次是哪一天"，再转回 UTC——不能直接在 UTC 上加 24/168 小时：
    有夏令时的时区（比如 America/New_York）春天/秋天调表的那一天，本地一天不是精确的 24
    小时，直接在 UTC 上加整数天会让提醒的本地时刻悄悄漂移一小时；Asia/Shanghai 没有夏令时，
    两种算法在这上面看不出区别，所以专门用非 Asia/Shanghai 时区的用例覆盖这一点。
    """
    tz = resolve_timezone(timezone_name)
    local_dt = event_at_utc.astimezone(tz)
    next_local = _advance_local_date(local_dt, repeat)
    return next_local.astimezone(timezone.utc)


def advance_after_trigger(
    event_at_utc: datetime,
    timezone_name: str,
    repeat: RepeatRule,
    advance_minutes: int,
    now_utc: datetime,
) -> Optional[tuple[datetime, datetime]]:
    """提醒推送完一次之后，算下一次的 (event_at, next_trigger_at)。

    repeat="none" 返回 None，调用方（scheduler）应该把这条提醒标成 done，不再往下算。

    错过好几次（比如 scheduler 停了一段时间）时不会把错过的每一次都补上：while 循环持续
    往前推进，直到算出的 next_trigger_at 落在未来为止，只补推一次（PHASE3.md 关键设计决定 5）。
    """
    if repeat == "none":
        return None

    next_event = compute_next_occurrence(event_at_utc, timezone_name, repeat)
    next_trigger = next_event - timedelta(minutes=advance_minutes)
    while next_trigger <= now_utc:
        next_event = compute_next_occurrence(next_event, timezone_name, repeat)
        next_trigger = next_event - timedelta(minutes=advance_minutes)
    return next_event, next_trigger
