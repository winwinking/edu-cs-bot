"""gateway 自己的 Prometheus 指标。"""
from prometheus_client import Counter, Gauge, Histogram

ws_connections = Gauge("gateway_ws_connections", "当前 WebSocket 连接数")

# 按处理结果分：accepted / duplicate / error，方便看出去重和失败各占多少
inbound_messages_total = Counter(
    "gateway_inbound_messages_total", "入站消息数，按处理结果分", ["status"]
)

ack_latency_seconds = Histogram("gateway_ack_latency_seconds", "从收到消息到回 ack 的耗时（秒）")
