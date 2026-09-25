"""worker 自己的 Prometheus 指标。"""
from prometheus_client import Counter, Gauge, Histogram

# result 取值固定是阶段一定下来的这五个，阶段三的错误率统计、阶段四的压测报告都靠这个标签算：
# ok（正常处理完）/ duplicate（去重跳过）/ forbidden（越权）/ llm_degraded（LLM 不可用，降级为
# 关键词规则兜底）/ dead_letter（不可预期异常，进了死信）。阶段二加了 intent 第二个标签，记录
# 跑完图之后具体路由到了哪个意图，方便按业务场景细分，但不影响 result 这条算错误率的主线
messages_total = Counter("worker_messages_total", "处理消息数，按结果分", ["result", "intent"])

process_seconds = Histogram("worker_process_seconds", "单条消息处理耗时（秒），含读历史、跑图、写库")
first_token_seconds = Histogram("worker_first_token_seconds", "LLM 首 token 耗时（秒）")
# 只统计 respond() 本身（组装/生成回复到发完 reply_end），跟 process_seconds 的区别是不含
# classify 阶段查数据库、调 LLM 判断意图那部分时间（阶段三第 6 步）
reply_seconds = Histogram("worker_reply_seconds", "生成并发送完整回复的耗时（秒）")

tool_calls_total = Counter("worker_tool_calls_total", "工具调用次数，按工具和结果分", ["tool", "status"])

# 消息级别的重试/死信计数（阶段三第 5 步），跟 messages_total{result="retry"/"dead_letter"}
# 统计的是同一件事，这里单独开两个指标方便直接画图，不用在 messages_total 上按 result 过滤
message_retry_total = Counter("worker_message_retry_total", "消息因不可预期异常重新入队的次数")
message_dead_letter_total = Counter("worker_message_dead_letter_total", "消息进死信的次数")

# 每 5 秒查一次 RabbitMQ 队列深度（阶段三第 6 步），不是每条消息都查一次——队列深度变化没那么快，
# 没必要为了这个指标给 RabbitMQ 增加额外负担
queue_backlog = Gauge("worker_queue_backlog", "队列里未处理的消息数，按队列名分", ["queue"])
