"""mock-llm 的确定性 tool_calls 规则（阶段二 2.5，阶段三第 2 步扩展了提醒规则）。

只在 mock-llm 里生效，用来验证 worker 那边"路由、校验、执行"这条管线对不对；接真实 DeepSeek
时，选哪个工具由真实模型决定，这套规则完全不参与。规则按顺序检查，先命中先用：
财务（R2）→ 平台指令（R3）→ 日程提醒（阶段三新增）→ 问候语 → 知识问答（R4，兜底）。
R2/R3/R4 的编号是阶段二定下来的历史编号，后面新加的规则不再重新排号，直接按检查顺序描述。
"""
import re
from datetime import date, datetime, timedelta
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
# "请假"中间常被口语插进量词/时间词，比如"请个假""请一天假"，不能只按字面"请假"两个连续字匹配
_LEAVE_RE = re.compile(r"请.{0,3}假")

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


def match_tool_call(content: str, *, now_local: Optional[datetime] = None) -> Optional[tuple[str, dict]]:
    """按 R1→R4 顺序检查，返回 (tool_name, args)；问候语和都不命中（R5）时返回 None，
    调用方应该走闲聊文字回复。

    now_local：worker 组装 classify 请求时会把"当前时间"写进 system prompt（PHASE3.md
    关键设计决定 6），这里从那句话里解析出来，用于把"明天 9 点"这类相对时间换算成具体日期，
    保证测试结果固定，不依赖 mock-llm 进程自己的系统时钟。传 None 时退回真实系统时钟
    （理论上不会发生，因为调用方一直会传，这里只是防御性兜底）。
    """
    user_text = _strip_meta_blocks(content)
    finance = _match_finance(user_text)
    if finance is not None:
        return finance
    # 平台指令排在提醒规则前面：PHASE2 已有的"修改课程提醒"（platform_command 的
    # update_course_reminder 动作）和阶段三新的"日程提醒"（manage_reminder）字面上都含
    # "提醒"两个字，"课程提醒"这个具体短语要留给平台指令处理，不能被更宽松的"含'提醒'就当
    # 日程提醒"规则抢先接住；平台指令要求同时有触发词+具体动作词，够精确，不会误吞真正的
    # 日程提醒请求（那些请求本身通常没有"课程表/学习报告/自动续费/课程提醒/请假"这几个词）
    platform = _match_platform_command(user_text)
    if platform is not None:
        return platform
    reminder = _match_reminder(user_text, content, now_local or datetime.now())
    if reminder is not None:
        return reminder
    if _is_greeting(user_text):
        return None
    return _match_knowledge(user_text)


# ---------- 元信息块：<提醒列表>/<历史摘要> 都是 worker 拼进 user 消息给 LLM 看的上下文，
# 不是用户真正说的话，在做"是不是在说 XX"这类判断之前必须先去掉，不然块里的字眼
# （"提醒列表""生效中的提醒"，或者摘要文本里恰好提到的历史话题）会让 R1-R4 对任何消息
# 都可能误判。真正要用块内容的地方（比如从 <提醒列表> 里挑 id）用没去块的 full_content。----------

_META_BLOCK_RE = re.compile(r"<(?:提醒列表|历史摘要)>.*?</(?:提醒列表|历史摘要)>", re.S)
_REMINDER_LIST_ID_RE = re.compile(r"id: ([0-9a-fA-F-]{36})")
_EXPLICIT_UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")

_REMINDER_CANCEL_WORDS = ("取消", "删除", "关掉", "不用提醒", "不要提醒", "别提醒")
_REMINDER_VIEW_WORDS = ("查看", "我的提醒", "有哪些提醒", "看看提醒", "都有什么提醒")
_REMINDER_UPDATE_WORDS = ("改成", "改到", "换成", "调整", "修改")

_REMINDER_DATE_OFFSET = (("后天", 2), ("明天", 1), ("今天", 0), ("今晚", 0))
_REMINDER_TIME_RE = re.compile(r"(\d{1,2})\s*[:：点]\s*(\d{0,2})")
_REMINDER_PM_MARKERS = ("下午", "晚上")
_REMINDER_REPEAT_WORDS = (("工作日", "workdays"), ("每周", "weekly"), ("每天", "daily"))
_REMINDER_ADVANCE_RE = re.compile(r"提前\s*(\d{1,4})\s*分钟")
_REMINDER_TITLE_AFTER_RE = re.compile(r"提醒我([^，。！？,、]+)")
_REMINDER_TITLE_BEFORE_RE = re.compile(r"的([^，。！？\s]{1,20}?)提醒")


def _strip_meta_blocks(content: str) -> str:
    return _META_BLOCK_RE.sub("", content).strip()


def _extract_reminder_id(user_text: str, full_content: str) -> Optional[str]:
    # 用户消息原文里直接出现一个 UUID（比如故意拿别人的提醒 id 来测越权），优先采用；
    # 否则看 <提醒列表> 块里有没有唯一一条，唯一才能替用户"选"，多条/零条交给业务节点处理
    explicit = _EXPLICIT_UUID_RE.search(user_text)
    if explicit:
        return explicit.group()
    ids = _REMINDER_LIST_ID_RE.findall(full_content)
    return ids[0] if len(ids) == 1 else None


_REMINDER_LIST_ENTRY_DATE_RE = re.compile(
    r"id: ([0-9a-fA-F-]{36}) \| 标题：.*? \| 时间：(\d{4}-\d{2}-\d{2}) \d{2}:\d{2}"
)


def _reminder_original_date(reminder_id: Optional[str], full_content: str) -> Optional[str]:
    """从 <提醒列表> 块里查这个 id 原来的日期（YYYY-MM-DD）。修改提醒时，如果用户只说了
    时间没说日期，应该保留原提醒的日期，不能默认成"今天"——这是人审时发现的真实 bug：
    "明天 09:00 打扫房间"改成"晚上 8 点"，被错误地算成了"今天 20:00"，正确应为"明天 20:00"。
    """
    if reminder_id is None:
        return None
    return dict(_REMINDER_LIST_ENTRY_DATE_RE.findall(full_content)).get(reminder_id)


def _extract_reminder_date_offset(text: str) -> Optional[int]:
    for word, offset in _REMINDER_DATE_OFFSET:
        if word in text:
            return offset
    return None


def _extract_reminder_time_of_day(text: str) -> Optional[tuple[int, int]]:
    match = _REMINDER_TIME_RE.search(text)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2)) if match.group(2) else 0
    if any(marker in text for marker in _REMINDER_PM_MARKERS) and hour < 12:
        hour += 12
    return hour, minute


def _compute_reminder_event_time(
    text: str, now_local: datetime, *, default_date: Optional[str] = None
) -> Optional[str]:
    """default_date 只在"修改"场景传：用户只给了时间、没提哪天时，应该保留目标提醒原来的
    日期，不能像创建场景那样默认成"今天"（创建没有"原来的日期"这个概念，默认今天/自动挪到
    明天是唯一合理的行为，所以 create 分支调用这个函数时不传 default_date）。
    """
    time_of_day = _extract_reminder_time_of_day(text)
    if time_of_day is None:
        return None
    hour, minute = time_of_day
    offset = _extract_reminder_date_offset(text)
    if offset is not None:
        candidate = (now_local + timedelta(days=offset)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    elif default_date is not None:
        base_date = datetime.strptime(default_date, "%Y-%m-%d")
        candidate = base_date.replace(hour=hour, minute=minute, second=0, microsecond=0)
    else:
        # 没显式说哪天，也没有"原来的日期"可以沿用（创建场景）：默认今天；这个点已经过了就
        # 自动挪到明天（"每天 9 点"这类重复提醒不用每次都精确报日期）
        candidate = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now_local:
            candidate += timedelta(days=1)
    return candidate.strftime("%Y-%m-%d %H:%M")


def _extract_reminder_repeat(text: str) -> Optional[str]:
    for word, rule in _REMINDER_REPEAT_WORDS:
        if word in text:
            return rule
    return None


def _extract_reminder_advance_minutes(text: str) -> Optional[int]:
    match = _REMINDER_ADVANCE_RE.search(text)
    return int(match.group(1)) if match else None


def _extract_reminder_title(text: str) -> str:
    match = _REMINDER_TITLE_AFTER_RE.search(text)
    if match:
        title = match.group(1).strip("，。！？ ")
        if title:
            return title
    match = _REMINDER_TITLE_BEFORE_RE.search(text)
    if match:
        return match.group(1).strip()
    return "提醒"


def _match_reminder(user_text: str, full_content: str, now_local: datetime) -> Optional[tuple[str, dict]]:
    if "提醒" not in user_text:
        return None

    if any(word in user_text for word in _REMINDER_CANCEL_WORDS):
        args: dict = {"action": "cancel"}
        reminder_id = _extract_reminder_id(user_text, full_content)
        if reminder_id:
            args["reminder_id"] = reminder_id
        return "manage_reminder", args

    if any(word in user_text for word in _REMINDER_VIEW_WORDS):
        return "manage_reminder", {"action": "view"}

    if any(word in user_text for word in _REMINDER_UPDATE_WORDS):
        args = {"action": "update"}
        reminder_id = _extract_reminder_id(user_text, full_content)
        if reminder_id:
            args["reminder_id"] = reminder_id
        default_date = _reminder_original_date(reminder_id, full_content)
        event_time = _compute_reminder_event_time(user_text, now_local, default_date=default_date)
        if event_time:
            args["event_time"] = event_time
        repeat = _extract_reminder_repeat(user_text)
        if repeat:
            args["repeat"] = repeat
        advance_minutes = _extract_reminder_advance_minutes(user_text)
        if advance_minutes is not None:
            args["advance_minutes"] = advance_minutes
        return "manage_reminder", args

    # 默认按创建处理
    args = {"action": "create", "title": _extract_reminder_title(user_text)}
    event_time = _compute_reminder_event_time(user_text, now_local)
    if event_time:
        args["event_time"] = event_time
    repeat = _extract_reminder_repeat(user_text)
    if repeat:
        args["repeat"] = repeat
    advance_minutes = _extract_reminder_advance_minutes(user_text)
    if advance_minutes is not None:
        args["advance_minutes"] = advance_minutes
    return "manage_reminder", args


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
    if _LEAVE_RE.search(content):
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


_HISTORY_SUMMARY_MARKER = "历史摘要生成"


def is_history_summary_request(system_content: str) -> bool:
    """请求不带 tools 时用：识别是不是"生成历史摘要"请求（阶段三第 3 步）。

    跟转人工摘要不同，这个标记词要求出现在 system 消息里，不是用户/对话内容——历史摘要
    生成请求本身就是把一堆对话原文塞进 user 消息，如果按 user 内容判断标记词，用户聊天里
    只要恰好提到类似的字眼就会被误判，所以调用方传的是 system 消息内容，不是最后一条用户消息。
    """
    return _HISTORY_SUMMARY_MARKER in system_content


_CURRENT_TIME_RE = re.compile(r"当前时间：(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})")


def extract_current_time(system_content: str) -> Optional[datetime]:
    """从 system prompt 里解析 worker 写进去的"当前时间"（app.worker.graph.style.
    build_current_time_note，PHASE3.md 关键设计决定 6）。解析不到返回 None，调用方应该退回
    真实系统时钟——正常链路里这句话一直会被写进去，解析不到属于异常情况的兜底，不是常态。
    """
    match = _CURRENT_TIME_RE.search(system_content)
    if not match:
        return None
    year, month, day, hour, minute = (int(g) for g in match.groups())
    return datetime(year, month, day, hour, minute)
