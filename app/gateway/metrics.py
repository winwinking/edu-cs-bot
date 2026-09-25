"""gateway 自己的 Prometheus 指标。"""
from prometheus_client import Counter, Gauge, Histogram

ws_connections = Gauge("gateway_ws_connections", "当前 WebSocket 连接数")

# 按机构和处理结果分（阶段三第 6 步：NFR-5 要求按租户统计）：accepted / duplicate /
# rate_limited / error，方便看出去重、限流、失败各占多少，按机构拆开看有没有某个机构
# 单独出问题。只按 tenant 不按 user_id（设计决定 14：避免指标基数失控）
inbound_messages_total = Counter(
    "gateway_inbound_messages_total", "入站消息数，按机构和处理结果分", ["tenant_id", "status"]
)

ack_latency_seconds = Histogram("gateway_ack_latency_seconds", "从收到消息到回 ack 的耗时（秒）")
