"""覆盖故障注入 11 确认后续人审发现：`trace_id`/`conversation_id` 这类十六进制 id 如果恰好
连续出现 12 位以上纯数字字符，会被 `app.common.masking` 的银行卡正则误判打码，导致同一个
trace_id 有时候在日志里搜得到、有时候搜不到。`_mask_value` 现在按字段名（`*_id`/`id`）跳过
内容正则，这里验证这条例外规则本身、以及它不会误伤真正需要脱敏的字段。
"""
from app.common.logging import _mask_value, desensitize_processor


def test_trace_id_with_long_digit_run_survives_masking():
    # 中间连续 12 位纯数字，命中 masking._BANK_CARD_RE 的匹配条件（脱敏前会被打码成"...尾号 xxxx"）
    trace_id = "a1b2345678901234c5d6"
    assert _mask_value(trace_id, key="trace_id") == trace_id


def test_conversation_id_with_all_digit_uuid_segment_survives_masking():
    conversation_id = "12345678-1234-1234-1234-123456789012"
    assert _mask_value(conversation_id, key="conversation_id") == conversation_id


def test_bare_id_field_name_survives_masking():
    assert _mask_value("123456789012345", key="id") == "123456789012345"


def test_other_id_suffixed_fields_survive_masking():
    for key in ("message_id", "pending_action_id", "handoff_ticket_id", "reminder_id", "doc_id"):
        value = "123456789012345"
        assert _mask_value(value, key=key) == value


def test_id_exemption_does_not_bypass_sensitive_field_name_redaction():
    # 假设字段名同时命中 *_id 后缀和敏感关键词（比如 bank_card_id），仍然要整体打码，
    # 不能因为加了 id 例外反而漏了这类字段
    assert _mask_value("6222021234567890", key="bank_card_id") == "***REDACTED***"


def test_non_id_field_with_long_digit_run_is_still_masked():
    # 确认这条例外只对 id 字段生效，普通自由文本字段该脱敏还是要脱敏
    assert _mask_value("这是一串卡号6222021234567890", key="detail") == "这是一串卡号尾号 7890"


def test_desensitize_processor_keeps_trace_id_intact_end_to_end():
    event_dict = {
        "event": "示例日志",
        "trace_id": "a1b234567890123c5d6",
        "detail": "银行卡 6222021234567890 验证失败",
    }
    result = desensitize_processor(None, "info", dict(event_dict))
    assert result["trace_id"] == event_dict["trace_id"]
    assert "6222021234567890" not in result["detail"]
