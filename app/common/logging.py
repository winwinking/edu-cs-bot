"""structlog 配置：JSON 输出，trace_id/tenant_id/conversation_id 用 contextvars 自动挂到每条日志上，
并在渲染前脱敏，防止手机号/身份证/银行卡/邮箱/JWT 这类敏感信息原样进日志。

脱敏分两层：
1. 按字段名——只要 key 里包含 password/token/phone/email/id_card/bank_card 这类词，不管值长什么样，整体打码。
   这是第一道防线，因为业务字段名是我们自己定的，比正则猜值的格式更可靠。
2. 按内容正则——防止敏感信息混在一段普通文本里（比如 detail: "手机号 138xxx 验证失败"），
   字段名那层不会覆盖到这种情况，所以正则作为兜底，两层都要。这一层复用 app.common.masking
   （阶段二 2.9 财务回复脱敏用的同一套函数），日志和财务回复的脱敏格式不会出现两套标准。

故障注入 11 确认后续人审发现：第 2 条规则的银行卡正则 `\\d{8,15}(\\d{4})`
（app.common.masking._BANK_CARD_RE）只看"是不是一串连续数字"，不看字段是什么——`trace_id`/
`conversation_id` 这类十六进制 id（`uuid.uuid4().hex`）如果恰好连续出现 12 位以上纯数字
字符（字母 a-f 会打断连续数字，但落在这个区间纯靠运气），会被这条正则当成银行卡号打码成
"...尾号 XXXX"，导致同一个 trace_id 有时候能在日志里搜到、有时候搜不到，且没有任何规律，
排障时非常难定位。这类 `*_id`/`id` 字段本身就是结构化标识符，不是自由文本，不应该被内容正则
猜测；已加 `_ID_FIELD_RE` 按字段名跳过内容正则（这类字段永远不会真的是手机号/邮箱/银行卡，
跳过没有漏检风险）。
"""
import logging
import re
from typing import Any

import structlog

from app.common.config import get_settings
from app.common.masking import mask_text as _mask_sensitive_text

_JWT_RE = re.compile(r"\bey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b")

# 字段名脱敏用的关键词，大小写不敏感、只要 key 里包含就命中（如 access_token、user_phone、bank_card_no）
_SENSITIVE_FIELD_RE = re.compile(r"(password|token|phone|email|id_card|bank_card)", re.IGNORECASE)
_REDACTED_FIELD = "***REDACTED***"

# trace_id/conversation_id/message_id/pending_action_id/handoff_ticket_id/reminder_id/
# doc_id 这类结构化标识符字段——按这个仓库统一的命名习惯，全部以 `_id` 结尾或者就叫 `id`，
# 这类字段的值永远是 UUID/hex 字符串，不是自由文本，跳过内容正则不会漏掉真实的手机号/邮箱/
# 银行卡（那些字段不会叫这个名字）。放在 _SENSITIVE_FIELD_RE 判断之后生效——如果哪个字段名
# 同时命中两条规则（比如假设出现过 bank_card_id 这种名字），先按整体打码的规则处理，不会因为
# 加了这条例外反而漏敏感信息
_ID_FIELD_RE = re.compile(r"(^id$|_id$)", re.IGNORECASE)


def _mask_text(value: str) -> str:
    # JWT 是日志场景特有的（token 形态字符串），跟财务脱敏无关，留在这里单独处理
    value = _JWT_RE.sub("***REDACTED_TOKEN***", value)
    return _mask_sensitive_text(value)


def _mask_value(value: Any, key: str | None = None) -> Any:
    if isinstance(value, str):
        # 字段名命中敏感关键词：不管内容是什么格式，直接整体打码，不依赖内容正则猜得准不准
        if key and _SENSITIVE_FIELD_RE.search(key):
            return _REDACTED_FIELD
        # 结构化 id 字段：跳过内容正则，避免十六进制 id 里偶然连续出现的数字串被银行卡正则
        # 误判打码，导致同一个 trace_id 有时候搜得到、有时候搜不到
        if key and _ID_FIELD_RE.search(key):
            return value
        return _mask_text(value)
    if isinstance(value, dict):
        return {k: _mask_value(v, key=k) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_mask_value(v, key=key) for v in value]
    return value


def desensitize_processor(logger: Any, method_name: str, event_dict: dict) -> dict:
    for key, value in list(event_dict.items()):
        event_dict[key] = _mask_value(value, key=key)
    return event_dict


def configure_logging() -> None:
    """进程启动时调用一次。日志始终是 JSON，方便后续接日志采集系统。"""
    settings = get_settings()
    logging.basicConfig(format="%(message)s", level=settings.log_level)

    # httpx/httpcore（openai SDK 底层用它们发请求）用标准库 logging 直接打印一行纯文本的
    # "HTTP Request: ..."，不走 structlog 这套处理链——不带 trace_id/tenant_id，格式也不是
    # JSON，混进结构化日志流里既是噪音又破坏"日志始终是 JSON"这条约定（PHASE4.md 4.6 人审
    # 发现）。这两个 logger 调到 WARNING，只保留真正的错误，正常请求不再打印
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            desensitize_processor,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(settings.log_level)),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> Any:
    return structlog.get_logger(name)


def bind_trace_context(
    *,
    trace_id: str | None = None,
    tenant_id: str | None = None,
    conversation_id: str | None = None,
) -> None:
    """把这条链路的标识塞进 contextvars，后面这个协程/线程里所有日志自动带上，不用每次手动传"""
    fields = {}
    if trace_id is not None:
        fields["trace_id"] = trace_id
    if tenant_id is not None:
        fields["tenant_id"] = tenant_id
    if conversation_id is not None:
        fields["conversation_id"] = conversation_id
    structlog.contextvars.bind_contextvars(**fields)


def clear_trace_context() -> None:
    structlog.contextvars.clear_contextvars()
