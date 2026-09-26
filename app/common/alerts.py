"""最小告警（PHASE4.md 4.5，关键设计决定 5）：只覆盖题目 6.5 点名要验证的三个触发点——
熔断打开、消息进死信、Redis 连接失败。不接钉钉/邮件/短信这类真实告警通道（写进已知问题），
先把"发生了这件事"稳定地记下来：一条 `event=alert`、`level=error` 的结构化日志 + 一个
按 alert_type 分类的 Prometheus 计数器，以后接哪个通道都能直接从这两个地方拿数据接进去。

三处触发点都调用这一个函数，是为了保证格式统一——不用每处自己拼日志字段，也不会有的地方
漏带 alert_type。trace_id/tenant_id 不在这里手动传：调用点通常已经在
`bind_trace_context()` 绑定过的协程上下文里（gateway/worker 处理一条消息、scheduler 推送
一条提醒），structlog 的 contextvars 处理器会自动把它们加进这条日志，不需要重复传一遍；
确实没绑定上下文时（比如进程刚启动就检测到 Redis 连不上），这条日志就没有这两个字段，
不硬凑假值。
"""
from prometheus_client import Counter

from app.common.logging import get_logger

logger = get_logger(__name__)

alerts_total = Counter("alerts_total", "触发的告警次数，按告警类型分", ["alert_type"])


def raise_alert(alert_type: str, **fields) -> None:
    alerts_total.labels(alert_type=alert_type).inc()
    logger.error("alert", alert_type=alert_type, **fields)
