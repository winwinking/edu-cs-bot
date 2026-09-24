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

# R3：触发平台指令的口语前缀
_PLATFORM_TRIGGER_ANY = ("帮我", "给我", "替我", "请帮")

# R4：问句特征词
_QUESTION_FEATURE_ANY = ("吗", "怎么", "多久", "能不能", "是否", "什么", "规则", "政策")

# 财务参数里 target_user_id 的格式：u_<租户字母>_<数字>，如 u_a_1002
_TARGET_USER_ID_RE = re.compile(r"u_[a-z]_\d+")


def match_tool_call(content: str) -> Optional[tuple[str, dict]]:
    """按 R1→R4 顺序检查，返回 (tool_name, args)；都不命中（R5）返回 None，调用方应该走闲聊文字回复"""
    return (
        _match_reminder(content)
        or _match_finance(content)
        or _match_platform_command(content)
        or _match_knowledge(content)
    )


def _match_reminder(content: str) -> Optional[tuple[str, dict]]:
    # R1：含"提醒"
    if "提醒" in content:
        return "manage_reminder", {"action": "create", "raw_text": content}
    return None


def _match_finance(content: str) -> Optional[tuple[str, dict]]:
    # R2：含财务词 + 含(我/帮/查)之一 + 不含(规则/政策/怎么/多久/流程/说明)任一
    kind = next((k for word, k in _FINANCE_KIND_MAP if word in content), None)
    if kind is None:
        return None
    if not any(word in content for word in _FINANCE_REQUIRE_ANY):
        return None
    if any(word in content for word in _FINANCE_EXCLUDE_ANY):
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
    # R4：含问句特征
    if any(word in content for word in _QUESTION_FEATURE_ANY):
        return "search_knowledge", {"query": content}
    return None


_MATERIAL_RE = re.compile(r"<资料>(.*?)</资料>", re.S)
_HANDOFF_SUMMARY_MARKER = "转人工摘要"


def extract_first_material(content: str) -> Optional[str]:
    """请求不带 tools 时用：取 <资料> 块的第一条正文（2.8 知识问答生成请求会带这个块）"""
    match = _MATERIAL_RE.search(content)
    return match.group(1).strip() if match else None


def is_handoff_summary_request(content: str) -> bool:
    """请求不带 tools 时用：识别是不是转人工摘要请求（2.11 会在 prompt 里带上这个标记词）"""
    return _HANDOFF_SUMMARY_MARKER in content
