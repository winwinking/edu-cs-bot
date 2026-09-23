"""structlog 配置：JSON 输出，trace_id/tenant_id/conversation_id 用 contextvars 自动挂到每条日志上，
并在渲染前脱敏，防止手机号/身份证/银行卡/邮箱/JWT 这类敏感信息原样进日志。

脱敏分两层：
1. 按字段名——只要 key 里包含 password/token/phone/email/id_card/bank_card 这类词，不管值长什么样，整体打码。
   这是第一道防线，因为业务字段名是我们自己定的，比正则猜值的格式更可靠。
2. 按内容正则——防止敏感信息混在一段普通文本里（比如 detail: "手机号 138xxx 验证失败"），
   字段名那层不会覆盖到这种情况，所以正则作为兜底，两层都要。
"""
import logging
import re
from typing import Any

import structlog

from app.common.config import get_settings

# 顺序有讲究：先匹配更"具体"的模式（邮箱、JWT、身份证），再匹配容易和别的数字串混淆的手机号/银行卡，
# 避免一个 18 位身份证号先被手机号规则误处理成一半脱敏一半不脱敏
_EMAIL_RE = re.compile(r"([\w.+-])[\w.+-]*(@[\w-]+\.[\w.-]+)")
_JWT_RE = re.compile(r"\bey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b")
_ID_CARD_RE = re.compile(r"\b(\d{6})\d{8}(\d{3}[\dXx])\b")
_BANK_CARD_RE = re.compile(r"\b(\d{4})\d{8,11}(\d{4})\b")
_PHONE_RE = re.compile(r"\b(1[3-9]\d)\d{4}(\d{4})\b")

# 字段名脱敏用的关键词，大小写不敏感、只要 key 里包含就命中（如 access_token、user_phone、bank_card_no）
_SENSITIVE_FIELD_RE = re.compile(r"(password|token|phone|email|id_card|bank_card)", re.IGNORECASE)
_REDACTED_FIELD = "***REDACTED***"


def _mask_text(value: str) -> str:
    value = _JWT_RE.sub("***REDACTED_TOKEN***", value)
    value = _EMAIL_RE.sub(r"\1***\2", value)
    value = _ID_CARD_RE.sub(r"\1********\2", value)
    value = _PHONE_RE.sub(r"\1****\2", value)
    value = _BANK_CARD_RE.sub(r"\1********\2", value)
    return value


def _mask_value(value: Any, key: str | None = None) -> Any:
    if isinstance(value, str):
        # 字段名命中敏感关键词：不管内容是什么格式，直接整体打码，不依赖内容正则猜得准不准
        if key and _SENSITIVE_FIELD_RE.search(key):
            return _REDACTED_FIELD
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
