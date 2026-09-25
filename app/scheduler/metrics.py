"""scheduler 自己的 Prometheus 指标（阶段三第 6 步）。"""
from prometheus_client import Counter, Histogram

reminder_push_total = Counter("scheduler_reminder_push_total", "提醒推送次数，按结果分", ["result"])
# 实际推送时间 - next_trigger_at：能看出到期之后隔了多久才真的推出去，正常应该在 1 个
# scheduler_interval_seconds 左右，明显变大说明 scheduler 处理不过来或者卡住了
reminder_push_delay_seconds = Histogram("scheduler_reminder_push_delay_seconds", "提醒推送延迟（秒）")
