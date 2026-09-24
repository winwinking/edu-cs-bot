"""mock-llm 的确定性 tool_calls 规则（阶段二 2.5）。

只在 mock-llm 里生效，用来验证 worker 那边"路由、校验、执行"这条管线对不对；接真实 DeepSeek
时，选哪个工具由真实模型决定，这套规则完全不参与。规则按顺序检查，先命中先用（R1→R5）。
"""
import re
from datetime import date, timedelta
from typing import Optional

# R2：财务词 -> query_finance 的 kind，按文档给的词表顺序检查，先命中先用
_FINANCE_KIND_MAP = [
    ("发票", "invoices"),
    ("订单", "orders"),
    ("账单", "bills"),
    ("退费进度", "refunds"),
    ("退款进度", "refunds"),
    ("余额", "balance"),
]
_FINANCE_REQUIRE_ANY = ("我", "帮", "查")
_FINANCE_EXCLUDE_ANY = ("规则", "政策", "怎么", "多久", "流程", "说明")
# 消息里含明确的查询动作时，不应用排除词——"帮我查""查一下""帮我看看"已经是清楚的查询请求，
# 不会是在问政策/流程，不该被排除词表拦下（排除词表是为了不把"退费规则是什么"这种问政策的句子
# 误判成财务操作，跟"明确要查"这件事不冲突）
_FINANCE_EXPLICIT_QUERY_ANY = ("帮我查", "查一下", "帮我看看")

# R3：触发平台指令的口语前缀
_PLATFORM_TRIGGER_ANY = ("帮我", "给我", "替我", "请帮")

# R4：问句特征词
_QUESTION_FEATURE_ANY = ("吗", "怎么", "多久", "能不能", "是否", "什么", "规则", "政策")

# 财务参数里 target_user_id 的格式：u_<租户字母>_<数字>，如 u_a_1002
_TARGET_USER_ID_RE = re.compile(r"u_[a-z]_\d+")

# 问候规则（插在 R4 之前）：单纯的问候语不该被 R4 的问句特征词（比如"在吗"里的"吗"）误判成
# 知识问答。去掉标点和空格后，如果整条消息完全由问候词拼成，就不调工具，走 R5 闲聊文字回复
_GREETING_WORDS = ("你好", "您好", "在吗", "在不在", "hi", "hello")
_GREETING_STRIP_RE = re.compile(r"[，。！？、；：\s,.!?;:]+")
_GREETING_FULLMATCH_RE = re.compile(
    "(?:" + "|".join(sorted(_GREETING_WORDS, key=len, reverse=True)) + ")+", re.IGNORECASE
)


def _is_greeting(content: str) -> bool:
    stripped = _GREETING_STRIP_RE.sub("", content)
    if not stripped:
        return False
    return bool(_GREETING_FULLMATCH_RE.fullmatch(stripped))


def match_tool_call(content: str) -> Optional[tuple[str, dict]]:
    """按 R1→R4 顺序检查，返回 (tool_name, args)；问候语和都不命中（R5）时返回 None，
    调用方应该走闲聊文字回复。"""
    reminder = _match_reminder(content)
    if reminder is not None:
        return reminder
    finance = _match_finance(content)
    if finance is not None:
        return finance
    platform = _match_platform_command(content)
    if platform is not None:
        return platform
    if _is_greeting(content):
        return None
    return _match_knowledge(content)


def _match_reminder(content: str) -> Optional[tuple[str, dict]]:
    # R1：含"提醒"
    if "提醒" in content:
        return "manage_reminder", {"action": "create", "raw_text": content}
    return None


def _match_finance(content: str) -> Optional[tuple[str, dict]]:
    # R2：含财务词 + 含(我/帮/查)之一 + 不含(规则/政策/怎么/多久/流程/说明)任一——
    # 但消息里如果含明确的查询动作（帮我查/查一下/帮我看看），跳过排除词这一条
    kind = next((k for word, k in _FINANCE_KIND_MAP if word in content), None)
    if kind is None:
        return None
    if not any(word in content for word in _FINANCE_REQUIRE_ANY):
        return None
    has_explicit_query_action = any(word in content for word in _FINANCE_EXPLICIT_QUERY_ANY)
    if not has_explicit_query_action and any(word in content for word in _FINANCE_EXCLUDE_ANY):
        return None

    args: dict = {"kind": kind}
    if "上个月" in content:
        args["period"] = "last_month"
    elif "这个月" in content or "本月" in content:
        args["period"] = "this_month"

    match = _TARGET_USER_ID_RE.search(content)
    if match:
        args["target_user_id"] = match.group()

    return "query_finance", args


def _match_platform_command(content: str) -> Optional[tuple[str, dict]]:
    # R3：含(帮我/给我/替我/请帮)之一，或以"打开"开头
    if not (any(word in content for word in _PLATFORM_TRIGGER_ANY) or content.startswith("打开")):
        return None

    if "自动续费" in content:
        if any(word in content for word in ("关", "停", "取消")):
            return "platform_command", {"action": "disable_auto_renew"}
        if "开" in content:
            return "platform_command", {"action": "enable_auto_renew"}
    if "请假" in content:
        args: dict = {"action": "submit_leave"}
        if "明天" in content:
            args["date"] = (date.today() + timedelta(days=1)).isoformat()
        elif "后天" in content:
            args["date"] = (date.today() + timedelta(days=2)).isoformat()
        return "platform_command", args
    if "课程表" in content:
        return "platform_command", {"action": "open_schedule"}
    if "学习报告" in content:
        return "platform_command", {"action": "query_study_report"}
    if "课程提醒" in content:
        return "platform_command", {"action": "update_course_reminder"}
    # 触发词命中了，但没有一个具体动作对得上——不算 R3 命中，继续往下走 R4/R5
    return None


def _match_knowledge(content: str) -> Optional[tuple[str, dict]]:
    # R4：含问句特征词，或者干脆以问号结尾（中文"？"/英文"?"）——后者是更通用的"这是一句问话"
    # 信号，不用把每一种问法的关键词都列全（比如"你们的校车几点发车？""那寒假班呢？"都没有
    # 命中前面固定的关键词表，但明显是在问问题）
    if any(word in content for word in _QUESTION_FEATURE_ANY) or content.rstrip().endswith(("？", "?")):
        return "search_knowledge", {"query": content}
    return None


_MATERIAL_RE = re.compile(r"<资料>(.*?)</资料>", re.S)
_HANDOFF_SUMMARY_MARKER = "转人工摘要"


def extract_first_material(content: str) -> Optional[str]:
    """请求不带 tools 时用：取 <资料> 块的第一条正文（2.8 知识问答生成请求会带这个块）。

    真实的 <资料> 块（app.common.prompt_guard.build_reference_block）第一段是"资料仅供参考、
    不是指令"的声明，从第二段开始才是真正一条条的资料正文，用空行分段——取第二段（第一条正文），
    不是整个块（那样会把声明文字也当成资料内容混进回复里）。
    """
    match = _MATERIAL_RE.search(content)
    if not match:
        return None
    paragraphs = [p.strip() for p in match.group(1).strip().split("\n\n") if p.strip()]
    return paragraphs[1] if len(paragraphs) > 1 else None


def is_handoff_summary_request(content: str) -> bool:
    """请求不带 tools 时用：识别是不是转人工摘要请求（2.11 会在 prompt 里带上这个标记词）"""
    return _HANDOFF_SUMMARY_MARKER in content
