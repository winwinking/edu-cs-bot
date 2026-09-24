"""worker 自己的 Prometheus 指标。"""
from prometheus_client import Counter, Histogram

# result 取值固定是阶段一定下来的这五个，阶段三的错误率统计、阶段四的压测报告都靠这个标签算：
# ok（正常处理完）/ duplicate（去重跳过）/ forbidden（越权）/ llm_degraded（LLM 不可用，降级为
# 关键词规则兜底）/ dead_letter（不可预期异常，进了死信）。阶段二加了 intent 第二个标签，记录
# 跑完图之后具体路由到了哪个意图，方便按业务场景细分，但不影响 result 这条算错误率的主线
messages_total = Counter("worker_messages_total", "处理消息数，按结果分", ["result", "intent"])

process_seconds = Histogram("worker_process_seconds", "单条消息处理耗时（秒）")
first_token_seconds = Histogram("worker_first_token_seconds", "LLM 首 token 耗时（秒）")
