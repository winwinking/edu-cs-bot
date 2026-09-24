"""意图识别：规则先行，LLM 兜底（PHASE2.md 1.1）。

顺序（前面命中就不再往后走）：
1. 当前会话有未过期的待确认操作，且用户在确认/取消 -> confirm_action / cancel_action（规则）
2. 转人工关键词 -> handoff（规则）
3. 不满意关键词累计 2 次 -> handoff（规则）—— 具体的计数和话术是 2.11 的内容，这一步先不接，
   见 AGENT_LOG 步骤 2.7 的偏离说明
4. 敏感操作关键词 -> high_risk（规则），risk_flags 带 sensitive_request
5. 其余交给 LLM function calling：LLM 选了哪个工具就是哪个意图，没选工具就是 chitchat

LLM 调用失败或超时时，降级为 worker 自己的关键词规则（route_source=rule_fallback）；
关键词也判断不出来，就打上 fallback_reason=llm_unavailable，回复"LLM 不可用"固定话术。
"""
import re
import uuid

from openai import APIConnectionError, APIError, APITimeoutError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.llm_client import chat_completion
from app.common.logging import get_logger
from app.common.models import PendingAction, PendingActionStatus
from app.common.prompt_guard import detect_prompt_injection
from app.common.tools import ParsedToolCall, ToolCallError, parse_tool_call, to_openai_tools
from app.worker.graph.state import GraphState
from app.worker.graph.style import STYLE_SYSTEM_PROMPT

logger = get_logger(__name__)

# 转人工关键词（PHASE2.md 2.11）
HANDOFF_KEYWORDS = ("转人工", "人工客服", "找人工", "真人")

# 敏感操作关键词（PHASE2.md 2 高风险清单说明的例子）
SENSITIVE_KEYWORDS = ("注销账号", "改密码", "修改密码", "换绑手机", "解绑手机", "改银行卡", "更换银行卡")

# 确认/取消短句关键词（PHASE2.md 2.10）
_CONFIRM_KEYWORDS = ("确认",)
_CANCEL_KEYWORDS = ("取消", "算了", "不用了")
_CONFIRM_CANCEL_MAX_LEN = 8

_PUNCTUATION_RE = re.compile(r"[，。！？、；：“”‘’（）()\s,.!?;:'\"]+")

_TOOL_NAME_TO_INTENT = {
    "search_knowledge": "knowledge_qa",
    "query_finance": "finance_query",
    "platform_command": "platform_command",
    "manage_reminder": "reminder",
    "transfer_to_human": "handoff",
}

# worker 自己的关键词兜底规则（LLM 挂了时用），跟 mock-llm 里那套只用来测 mock 的规则是两回事：
# 这套在真接 DeepSeek 时也会用到，覆盖不到具体参数，只负责把意图先判出来，让业务节点先给用户一个
# 靠谱的占位/降级回复，而不是每次 LLM 一挂就统一回"系统这会儿有点忙"
_FINANCE_KEYWORDS = ("发票", "订单", "账单", "退费进度", "退款进度", "余额")
_PLATFORM_KEYWORDS = ("自动续费", "请假", "课程表", "学习报告", "课程提醒")
# 平台指令关键词要求同时出现口语触发词，否则"请假"这类词在提问句里（"寒假班请假会退课时费吗"）
# 会被误判成"要请假"，而不是在问请假政策
_PLATFORM_TRIGGER_ANY = ("帮我", "给我", "替我", "请帮")
_QUESTION_FEATURE_KEYWORDS = ("吗", "怎么", "多久", "能不能", "是否", "规则", "政策")


def _strip_punctuation(text: str) -> str:
    return _PUNCTUATION_RE.sub("", text)


async def _has_active_pending_action(session: AsyncSession, tenant_id: str, conversation_id: str) -> bool:
    stmt = (
        select(PendingAction.id)
        .where(
            PendingAction.tenant_id == tenant_id,
            PendingAction.conversation_id == uuid.UUID(conversation_id),
            PendingAction.status == PendingActionStatus.pending,
            PendingAction.expires_at > func.now(),
        )
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.first() is not None


def _keyword_fallback_classify(content: str) -> dict:
    if any(k in content for k in HANDOFF_KEYWORDS):
        return {"intent": "handoff", "route_source": "rule_fallback"}
    if any(k in content for k in SENSITIVE_KEYWORDS):
        return {"intent": "high_risk", "route_source": "rule_fallback", "risk_flags": ["sensitive_request"]}
    if any(k in content for k in _FINANCE_KEYWORDS) and any(k in content for k in ("我", "帮", "查")):
        return {"intent": "finance_query", "route_source": "rule_fallback"}
    if "提醒" in content:
        return {"intent": "reminder", "route_source": "rule_fallback"}
    if any(k in content for k in _PLATFORM_KEYWORDS) and (
        any(t in content for t in _PLATFORM_TRIGGER_ANY) or content.startswith("打开")
    ):
        return {"intent": "platform_command", "route_source": "rule_fallback"}
    if any(k in content for k in _QUESTION_FEATURE_KEYWORDS):
        return {"intent": "knowledge_qa", "route_source": "rule_fallback"}
    # 关键词也判断不出来：回复"LLM 不可用"固定话术，不是"LLM 输出非法"那句
    return {"intent": "fallback", "route_source": "rule_fallback", "fallback_reason": "llm_unavailable"}


def _tool_meta_entry(name: str, parsed: "ParsedToolCall | ToolCallError") -> dict:
    if isinstance(parsed, ToolCallError):
        return {"name": name, "status": parsed.reason}
    return {"name": name, "status": "ok"}


async def _classify_with_llm(state: GraphState) -> dict:
    messages = (
        [{"role": "system", "content": STYLE_SYSTEM_PROMPT}]
        + list(state.get("history", []))
        + [{"role": "user", "content": state["content"]}]
    )
    try:
        response = await chat_completion(messages=messages, tools=to_openai_tools(), tool_choice="auto")
    except (APIError, APITimeoutError, APIConnectionError) as exc:
        logger.warning("LLM 分类调用失败，降级为关键词规则", error=str(exc))
        return _keyword_fallback_classify(state["content"])

    message = response.choices[0].message
    tool_calls = message.tool_calls or []
    if not tool_calls:
        return {"intent": "chitchat", "route_source": "llm"}

    call = tool_calls[0]
    parsed = parse_tool_call(call.function.name, call.function.arguments or "{}")
    tools_meta = [_tool_meta_entry(call.function.name, parsed)]

    if isinstance(parsed, ToolCallError):
        logger.warning(
            "LLM 输出的工具调用没通过校验，走兜底", tool_name=call.function.name, reason=parsed.reason
        )
        return {
            "intent": "fallback",
            "route_source": "llm",
            "fallback_reason": "invalid_output",
            "tools_meta": tools_meta,
        }

    intent = _TOOL_NAME_TO_INTENT.get(parsed.name, "fallback")
    update: dict = {"intent": intent, "route_source": "llm", "tool_call": {"name": parsed.name, "args": parsed.args.model_dump(mode="json")}, "tools_meta": tools_meta}
    if parsed.is_high_risk:
        update["intent"] = "high_risk"
    return update


async def _classify_core(state: GraphState, session: AsyncSession, content: str) -> dict:
    # 1. 确认/取消（去掉标点后不超过 8 个字，且当前会话有未过期的待确认操作才生效）
    stripped = _strip_punctuation(content)
    if len(stripped) <= _CONFIRM_CANCEL_MAX_LEN:
        if await _has_active_pending_action(session, state["tenant_id"], state["conversation_id"]):
            if any(k in stripped for k in _CONFIRM_KEYWORDS):
                return {"intent": "confirm_action", "route_source": "rule"}
            if any(k in stripped for k in _CANCEL_KEYWORDS):
                return {"intent": "cancel_action", "route_source": "rule"}

    # 2. 转人工关键词
    if any(k in content for k in HANDOFF_KEYWORDS):
        return {"intent": "handoff", "route_source": "rule"}

    # 3. 不满意计数：阶段二 2.11 实现，这里先跳过

    # 4. 敏感操作关键词
    if any(k in content for k in SENSITIVE_KEYWORDS):
        return {"intent": "high_risk", "route_source": "rule", "risk_flags": ["sensitive_request"]}

    # 5. LLM function calling
    return await _classify_with_llm(state)


async def classify(state: GraphState, runtime) -> dict:
    session = runtime.context.session
    content = state["content"]
    result = await _classify_core(state, session, content)

    # Prompt injection 检测只打标记，不改变上面判出来的路由结果（2.6 app.common.prompt_guard 的
    # 检测函数一直没有实际接线，2.9 财务场景的验证要求补上）；不管命中哪条路由分支都要检查，
    # 所以放在最外层统一处理，而不是散在每个分支里各自判断一次
    if detect_prompt_injection(content):
        logger.warning("疑似 prompt injection，仅打标记，不影响正常的权限校验和白名单防线")
        risk_flags = list(result.get("risk_flags", []))
        if "prompt_injection_suspected" not in risk_flags:
            risk_flags.append("prompt_injection_suspected")
        result["risk_flags"] = risk_flags

    return result
