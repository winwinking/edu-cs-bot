"""LangGraph 编排的 state 定义和跑图时注入的依赖（PHASE2.md 2.7 第 1 点）。

state 只负责在节点之间传递"决策"用得到的数据，不放数据库 session 这类资源——资源通过
GraphContext（LangGraph 的 context_schema）传，节点用 runtime.context 拿，state 保持可序列化、
方便以后要接 LangGraph 的 checkpoint/调试工具时不会因为塞了不可序列化的对象而出问题。
"""
from dataclasses import dataclass
from typing import Any, List, Optional, TypedDict

from sqlalchemy.ext.asyncio import AsyncSession


class GraphState(TypedDict, total=False):
    tenant_id: str
    user_id: str
    role: str
    conversation_id: str
    message_id: str
    trace_id: Optional[str]

    # load_context 一并查出来，给 classify 组装 LLM 请求用（PHASE3.md 第 2 步）：tenant_timezone
    # 是"明天 9 点"这类相对时间换算的依据；reminder_list_block 是给 LLM 挑 id 用的 <提醒列表>
    # 文本，每条消息都会查一次（不管这条消息是不是在说提醒），换一次小查询换来的是分类阶段
    # 随时能看到最新的提醒状态，不用另外判断"这条消息像不像在说提醒"才决定要不要查
    tenant_timezone: Optional[str]
    reminder_list_block: Optional[str]
    # load_context 查一次放进来（阶段三第 3 步）：更早的对话压成的摘要，已脱敏；
    # classify/chitchat/knowledge 组装 user 消息时统一用 append_summary_block() 拼进去
    history_summary: Optional[str]

    content: str  # 当前这条用户消息
    history: List[dict]  # 历史消息（不含当前这条），[{"role": ..., "content": ...}]

    intent: Optional[str]
    route_source: Optional[str]  # rule | llm | rule_fallback
    # LLM 输出非法/调用失败时用来区分该用哪句固定话术：invalid_output（非法 JSON/未知工具/schema 不对）
    # 或 llm_unavailable（LLM 调用本身失败，关键词兜底也判断不出来）
    fallback_reason: Optional[str]

    tool_call: Optional[dict]  # {"name": str, "args": dict}，已经过 parse_tool_call 校验
    tools_meta: List[dict]  # meta.tools：本轮尝试解析/调用过的工具，[{"name", "status", ...}]

    reply_plan: Optional[dict]  # {"mode": "template", "text": ...} 或 {"mode": "generate", "messages": ...}
    citations: List[dict]
    risk_flags: List[str]

    pending_action_id: Optional[str]
    handoff_ticket_id: Optional[str]
    # handoff 节点写 handoff_tickets.trigger 用：keyword（转人工关键词）/ dissatisfied（不满意计数
    # 到阈值）/ llm（LLM 选了 transfer_to_human 工具），由 classify 按命中的分支设置
    handoff_trigger: Optional[str]

    # 本轮因为熔断打开而被跳过、没真的发请求的服务名（阶段三第 5 步，设计决定 10），
    # 比如 ["llm"]、["finance"]；给 reply_end 的 meta 用，客户端/演示控制台标出"熔断降级"
    circuit_breaker: List[str]

    # 本轮因为机构当天 token 预算用完而跳过 LLM 调用（阶段三第 6 步，设计决定 13）；
    # 给 reply_end 的 meta 用，跟 circuit_breaker 是并列但不同原因的两种"没真的调 LLM"
    budget_exceeded: bool

    # ---------- 阶段三 3.8 第二轮：演示控制台流程图回放用的真实打点 ----------
    # 只加计时和记录，不参与任何业务判断——这三个字段是"Jo 批准的第二处后端改动"（AGENT_LOG
    # 3.8 一节有原因和范围说明）。path 是这条消息实际经过的图节点名，按执行顺序追加（由
    # graph.py 的 _timed() 包装器统一打点，不需要每个节点自己维护）；timings 是每个节点自己的
    # 耗时（毫秒）；llm_ms 是这条消息里真正花在等 LLM（mock-llm/DeepSeek）网络调用上的时间，
    # 跟 timings 里某个节点的总耗时是两回事——一个节点里除了等 LLM，还有数据库查询、Guard
    # 处理这些非 LLM 的开销，llm_ms 单独摘出来才能在阶段四拆分"系统自己的耗时"和"mock/真实
    # LLM 的耗时"。
    path: List[str]
    timings: dict  # {node_name: 毫秒}
    llm_ms: float


@dataclass
class GraphContext:
    """跑一次图用得到的依赖，通过 `.ainvoke(..., context=GraphContext(session=session))` 注入。"""

    session: AsyncSession
