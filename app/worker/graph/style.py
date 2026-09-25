"""统一的风格 system prompt 和固定话术（PHASE2.md 2.7 第 8、9 点，对应 FR-8、附录 B）。"""
from datetime import datetime
from zoneinfo import ZoneInfo

# 用在所有 "generate" 模式的 LLM 调用上：先确认问题、再给结论、再给下一步；不确定就明说；
# 不用套话和表情。用户输入和检索资料永远只放在 user 消息里，这条 system prompt 本身是固定内容，
# 不会拼进任何用户输入或检索结果（硬性规则：用户输入不得拼进 system prompt）
STYLE_SYSTEM_PROMPT = (
    "你是一个教育平台的客服机器人，服务学员和家长。回答要求：\n"
    "1. 先确认清楚用户的问题，再给结论，最后给出下一步该怎么做；\n"
    "2. 语言专业、简洁、有人情味，不用“作为AI”“我很乐意”“总之”“希望对你有帮助”这类套话，不堆砌表情符号；\n"
    "3. 拿不准的地方要明确说不确定，并给出转人工或稍后回复的方案，不能编造信息；\n"
    "4. 涉及关闭自动续费、请假等操作，只负责说明情况，不要自己下结论说“已经办好了”。"
)

# LLM 输出非法（未知工具/参数不对/JSON 解析失败）时的兜底话术
FALLBACK_INVALID_OUTPUT_REPLY = (
    "这句话我没能准确理解，为了避免误操作，我先不做任何处理。你可以换个说法再说一次，或者回复“转人工”。"
)

# LLM 调用本身失败（超时/连不上/上游 500），关键词兜底也判断不出意图时的话术
FALLBACK_LLM_UNAVAILABLE_REPLY = "系统这会儿有点忙，我暂时没法处理这个问题。你可以稍后再试，或者回复“转人工”。"

# 命中敏感操作关键词（注销账号、改密码、换绑手机、改银行卡等）时的话术
SENSITIVE_REPLY = "这类操作涉及账号安全，需要人工核实身份后才能办理。回复“转人工”，我帮你转接。"

_WEEKDAY_CN = "一二三四五六日"


def build_current_time_note(timezone_name: str) -> str:
    """给 classify 阶段的 system prompt 追加"当前时间"（PHASE3.md 关键设计决定 6）：这不是用户
    输入，是代码算出来的事实，可以放进 system prompt；LLM（含 mock-llm）拿它来把"明天 9 点"
    这类相对时间换算成具体日期，不用、也不该用自己的系统时钟猜——那样测试结果没法固定。
    """
    now_local = datetime.now(ZoneInfo(timezone_name))
    weekday = _WEEKDAY_CN[now_local.weekday()]
    return f"当前时间：{now_local:%Y-%m-%d %H:%M}，星期{weekday}，时区 {timezone_name}。"


# 提醒相关固定话术（PHASE3.md 第 2 步）
REMINDER_CREATE_FAILED_REPLY = "提醒这次没设置成功，麻烦再发一次。"
REMINDER_NO_ACTIVE_REPLY = "你目前没有生效的提醒。"
REMINDER_FORBIDDEN_REPLY = "这条提醒不是你名下的，我不能操作。"

# 知识问答检索无命中（PHASE2.md 2.8 第 2 点）：不给 LLM 编的机会，直接回固定话术
KNOWLEDGE_NO_HIT_REPLY = "我暂时没有查到明确依据，建议转人工确认。回复“转人工”我帮你转接。"

# 不满意关键词第 1 次命中的道歉引导（PHASE2.md 2.11 第 1 点），第 2 次命中直接触发转人工，
# 不会再回这句话
DISSATISFIED_FIRST_REPLY = "抱歉刚才没帮上。你可以说一下具体哪里不对，或者回复“转人工”。"

# 财务查询越权 / 故障固定话术（PHASE2.md 2.9 第 5 点），金额/订单号只来自接口返回值，不进这两句
FINANCE_FORBIDDEN_REPLY = "这个账号的财务信息不属于你，我这边不能查询。如果需要，请本人登录后再问我。"
FINANCE_UPSTREAM_ERROR_REPLY = "财务系统暂时查不到你的信息，这次查询我已记录，稍后回复你。"

# 平台指令二次确认相关的固定话术（PHASE2.md 2.10 第 5、6 点）：具体动作是什么、确认话术怎么写
# 由 app/worker/graph/command.py 按 action 动态拼，这两句是跟具体动作无关的通用兜底
PLATFORM_ALREADY_PROCESSED_REPLY = "这个操作已经处理过了。"
PLATFORM_CONFIRM_TIMEOUT_REPLY = "确认已超时，操作没有执行，需要的话重新跟我说一次。"
# 低风险指令（打开课程表等）调用 mock-platform 失败时的兜底，跟财务查询故障话术是同一个设计：
# 不编结果、如实告知稍后再试
PLATFORM_LOW_RISK_ERROR_REPLY = "这个操作暂时办不了，我已经记录，你可以稍后再试。"

# 知识问答 system prompt 的追加部分（PHASE2.md 2.8 第 3 点）：只根据资料回答，出处由代码加，
# 不用 LLM 自己写——LLM 编出处这件事本来就防不住，干脆不让它写，交给 OutputGuard 逐句核对
KNOWLEDGE_SYSTEM_ADDENDUM = (
    "只根据下面提供的资料回答；资料里没写的内容要明确说没写，不能编造；"
    "不要自己写出处（比如“根据《xxx》第x条”），出处会由系统自动加在回答最前面；"
    "直接从结论开始说，不用重复用户的问题。"
)
