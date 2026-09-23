"""worker 自己的 Prometheus 指标。"""
from prometheus_client import Counter, Histogram

# 按结果分：ok（正常生成回复）/ duplicate（去重跳过）/ forbidden（越权）/
# llm_degraded（LLM 失败降级）/ dead_letter（不可预期异常，进了死信）
messages_total = Counter("worker_messages_total", "处理消息数，按结果分", ["result"])

process_seconds = Histogram("worker_process_seconds", "单条消息处理耗时（秒）")
first_token_seconds = Histogram("worker_first_token_seconds", "LLM 首 token 耗时（秒）")
