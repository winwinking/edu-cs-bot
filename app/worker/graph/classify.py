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
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.llm_client import chat_completion
from app.common.logging import get_logger
from app.common.models import Conversation, PendingAction, PendingActionStatus
from app.common.prompt_guard import detect_prompt_injection
from app.common.tools import ParsedToolCall, ToolCallError, parse_tool_call, to_openai_tools
from app.worker.graph.context_summary import append_summary_block
from app.worker.graph.state import GraphState
from app.worker.graph.style import STYLE_SYSTEM_PROMPT, build_current_time_note

logger = get_logger(__name__)

# 转人工关键词（PHASE2.md 2.11）
HANDOFF_KEYWORDS = ("转人工", "人工客服", "找人工", "真人")

# 敏感操作关键词（PHASE2.md 2 高风险清单说明的例子）
SENSITIVE_KEYWORDS = ("注销账号", "改密码", "修改密码", "换绑手机", "解绑手机", "改银行卡", "更换银行卡")

# 不满意关键词（PHASE2.md 2.11 第 1 点给的例子，"等" 意味着不是穷举，后续按实际客服日志再补）
DISSATISFIED_KEYWORDS = ("没用", "不对", "答非所问", "听不懂", "不满意")

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

_DISSATISFIED_THRESHOLD = 2

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
    """有没有一条还没过期、还没被处理的待确认——只用来判断"对/是的"这类模糊回应要不要走
    confirm_ambiguous 的提醒话术：待确认已经执行完或过期了，"对"就该当成普通消息正常处理，
    不该再提醒"要回复确认关闭"（都处理完了，提醒也没意义）。"""
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


async def _has_recent_pending_action(session: AsyncSession, tenant_id: str, conversation_id: str) -> bool:
    """这个会话有没有一条"还在有效期内"的待确认操作——不要求 status 还是 pending（跟上面那个
    "未过期"版本的区别只在这一点），已经在有效期内执行完/取消的也算。用来判断"确认关闭""算了"
    这类短句要不要路由去 confirm_action/cancel_action。

    这里必须带 expires_at 这个时间窗，不能是"这个会话有没有出现过待确认操作"（不管多久以前）：
    人审时验证过一个反例——会话里很久以前有一条已经执行完的操作，用户现在说"确认一下我的课表"，
    这句话里也含"确认"两个字，如果不限定时间窗，会被误判成在重复确认那条早就结束的旧操作，回复
    "这个操作已经处理过了"，这是错的，"确认"在这句话里就是"核对"的普通动词用法。限定在
    expires_at 这个窗口内（跟 PendingAction 自己的有效期一致），能覆盖"操作刚执行完，用户手快
    又确认了一次"（这时 expires_at 还没到，仍然要拦下来回复"已经处理过了"），又不会永久把这个
    会话的"确认"两个字焊死成"一定是在回应旧操作"。"""
    stmt = (
        select(PendingAction.id)
        .where(
            PendingAction.tenant_id == tenant_id,
            PendingAction.conversation_id == uuid.UUID(conversation_id),
            PendingAction.expires_at > func.now(),
        )
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.first() is not None


async def _bump_dissatisfied_count(
    session: AsyncSession, tenant_id: str, conversation_id: str, *, hit: bool
) -> int:
    """命中不满意关键词就 +1，其余消息清零（PHASE2.md 2.11 第 1 点）。用一条 UPDATE ... RETURNING
    做完"改值 + 拿到改完之后的值"，不用先 SELECT 再 UPDATE——这里没有确认执行那种"抢占"语义，
    单个会话基本不会有并发写，犯不上再上一把锁，跟 confirm_action 的原子抢占不是一回事。"""
    new_value = Conversation.dissatisfied_count + 1 if hit else 0
    stmt = (
        update(Conversation)
        .where(Conversation.tenant_id == tenant_id, Conversation.id == uuid.UUID(conversation_id))
        .values(dissatisfied_count=new_value)
        .returning(Conversation.dissatisfied_count)
    )
    result = await session.execute(stmt)
    count = result.scalar_one()
    await session.commit()
    return count


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
    # 当前时间是代码算出来的事实，不是用户输入，放进 system prompt 没问题（关键设计决定 6）；
    # <提醒列表> 块拼进 user 消息（不是 system），跟 <资料>/<历史摘要> 这类"资料性"内容一个
    # 位置——修改/取消提醒时 LLM 要从这里挑 id。两者都是 load_context 这一步统一查好放进
    # state 的，这里不用再连数据库
    tenant_timezone = state.get("tenant_timezone") or "Asia/Shanghai"
    system_content = f"{STYLE_SYSTEM_PROMPT}\n{build_current_time_note(tenant_timezone)}"
    user_content = append_summary_block(state["content"], state.get("history_summary"))
    reminder_block = state.get("reminder_list_block") or ""
    if reminder_block:
        user_content = f"{user_content}\n\n{reminder_block}"
    messages = (
        [{"role": "system", "content": system_content}]
        + list(state.get("history", []))
        + [{"role": "user", "content": user_content}]
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
    update_fields: dict = {
        "intent": intent,
        "route_source": "llm",
        "tool_call": {"name": parsed.name, "args": parsed.args.model_dump(mode="json")},
        "tools_meta": tools_meta,
    }
    if parsed.is_high_risk:
        update_fields["intent"] = "high_risk"
    if parsed.name == "transfer_to_human":
        update_fields["handoff_trigger"] = "llm"
    return update_fields


async def _classify_core(state: GraphState, session: AsyncSession, content: str) -> dict:
    # 1. 确认/取消（去掉标点后不超过 8 个字才生效，防止正常长句里碰巧带"确认""取消"被误判）
    stripped = _strip_punctuation(content)
    is_short = len(stripped) <= _CONFIRM_CANCEL_MAX_LEN
    if is_short:
        has_confirm_keyword = any(k in stripped for k in _CONFIRM_KEYWORDS)
        has_cancel_keyword = any(k in stripped for k in _CANCEL_KEYWORDS)
        if has_confirm_keyword or has_cancel_keyword:
            # 含"确认"/取消词：这个会话最近（有效期内）出现过待确认操作就路由过去，不要求
            # status 还是 pending——操作已经在有效期内执行完，也要走 confirm_action/cancel_action，
            # 由它们自己查真实状态回复"已经处理过了"/"确认已超时"，而不是在这里因为状态不是
            # pending 就放过，让这句话落到 LLM 分类被误判成别的意图（mock-llm 对"确认关闭"没有
            # 任何规则命中，会被当成闲聊——这是 2.10 验证时实测发现的，见 AGENT_LOG）。用
            # "有效期内"而不是"不管多久以前"，是为了不把这个会话的"确认"两个字永久焊死成
            # "一定是在回应旧操作"——人审时验证过反例，见 _has_recent_pending_action 的注释
            if await _has_recent_pending_action(session, state["tenant_id"], state["conversation_id"]):
                return {
                    "intent": "confirm_action" if has_confirm_keyword else "cancel_action",
                    "route_source": "rule",
                }

    # 2. 转人工关键词
    if any(k in content for k in HANDOFF_KEYWORDS):
        return {"intent": "handoff", "route_source": "rule", "handoff_trigger": "keyword"}

    # 3. 不满意关键词计数（PHASE2.md 2.11 第 1 点）：命中就 +1，其它消息清零，累计到 2 次
    # 触发转人工并清零。放在转人工关键词之后——直接说"转人工"不算"不满意"，不参与这里的计数；
    # 这一步之前的两条分支（确认/取消、转人工关键词）命中时会直接 return，不会走到这里，
    # 所以这两类消息不会把计数器清零，只有"看起来是不满意，但也不是在确认/取消/直接要转人工"
    # 的消息才会真正触发这条计数逻辑——这是当前实现的范围，之后如果要覆盖到全部消息类型再调整
    dissatisfied_hit = any(k in content for k in DISSATISFIED_KEYWORDS)
    count = await _bump_dissatisfied_count(
        session, state["tenant_id"], state["conversation_id"], hit=dissatisfied_hit
    )
    if dissatisfied_hit:
        if count >= _DISSATISFIED_THRESHOLD:
            # 累计到阈值：触发转人工的同时清零，不然这条会话以后每一次不满意都会立刻转人工
            await _bump_dissatisfied_count(session, state["tenant_id"], state["conversation_id"], hit=False)
            return {"intent": "handoff", "route_source": "rule", "handoff_trigger": "dissatisfied"}
        return {"intent": "dissatisfied_first", "route_source": "rule"}

    # 4. 敏感操作关键词
    if any(k in content for k in SENSITIVE_KEYWORDS):
        return {"intent": "high_risk", "route_source": "rule", "risk_flags": ["sensitive_request"]}

    # 5. LLM function calling
    result = await _classify_with_llm(state)

    # 短句 + 有未过期的待确认操作 + LLM/mock-llm 也没判出任何真实意图（chitchat）——这才是
    # "对/是的/好的"这类模糊回应的通用特征，PHASE2.md 2.10 第 6 点要求提醒回复确切的确认短语，
    # 不能当成默认同意直接执行（误操作代价是真的执行一个高风险指令）。这个判断故意放在 LLM
    # 分类之后、只在结果是 chitchat 时才覆盖，而不是"短句+有未过期待确认"就直接短路——人审时
    # 验证过反例："发票多久能开"这种正常问题也是短句，如果不看 LLM 的分类结果就直接短路成
    # confirm_ambiguous，会把一个跟确认无关的正常问题错误地拦下来
    if is_short and result.get("intent") == "chitchat":
        if await _has_active_pending_action(session, state["tenant_id"], state["conversation_id"]):
            return {"intent": "confirm_ambiguous", "route_source": "rule"}

    return result


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
