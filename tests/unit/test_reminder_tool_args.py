"""覆盖 PHASE3.md 第 1 步的 manage_reminder 参数模型：四个动作字段是否必填、extra=forbid、
event_time 的格式校验。跟 app.common.reminder_rules 的语义校验（时区、是否在将来）是两层，
这里只测结构性校验这一层。
"""
from app.common.tools import ParsedToolCall, ToolCallError, parse_tool_call


def test_create_requires_title_and_event_time():
    result = parse_tool_call("manage_reminder", '{"action": "create"}')
    assert isinstance(result, ToolCallError)
    assert result.reason == "schema_error"


def test_create_success_with_title_and_event_time():
    result = parse_tool_call(
        "manage_reminder", '{"action": "create", "title": "交作业", "event_time": "2026-09-26 09:00"}'
    )
    assert isinstance(result, ParsedToolCall)
    assert result.args.title == "交作业"
    assert result.args.event_time == "2026-09-26 09:00"
    assert result.args.repeat is None
    assert result.args.advance_minutes is None
    assert result.is_high_risk is False


def test_create_with_repeat_and_advance_minutes():
    result = parse_tool_call(
        "manage_reminder",
        '{"action": "create", "title": "喝水", "event_time": "2026-09-26 09:00", '
        '"repeat": "daily", "advance_minutes": 10}',
    )
    assert isinstance(result, ParsedToolCall)
    assert result.args.repeat == "daily"
    assert result.args.advance_minutes == 10


def test_event_time_bad_format_is_rejected():
    result = parse_tool_call(
        "manage_reminder", '{"action": "create", "title": "交作业", "event_time": "明天九点"}'
    )
    assert isinstance(result, ToolCallError)
    assert result.reason == "schema_error"
    assert "event_time" in result.fields


def test_advance_minutes_out_of_range_is_rejected():
    result = parse_tool_call(
        "manage_reminder",
        '{"action": "create", "title": "交作业", "event_time": "2026-09-26 09:00", "advance_minutes": 2000}',
    )
    assert isinstance(result, ToolCallError)
    assert result.reason == "schema_error"
    assert "advance_minutes" in result.fields


def test_update_does_not_require_reminder_id_at_schema_layer():
    # reminder_id 缺失/选不出来是合法结果，交给业务节点按 0/1/多条分别处理，不在这一层拒绝
    result = parse_tool_call("manage_reminder", '{"action": "update", "event_time": "2026-09-26 20:00"}')
    assert isinstance(result, ParsedToolCall)
    assert result.args.reminder_id is None


def test_cancel_with_reminder_id():
    result = parse_tool_call(
        "manage_reminder", '{"action": "cancel", "reminder_id": "11111111-1111-1111-1111-111111111111"}'
    )
    assert isinstance(result, ParsedToolCall)
    assert result.args.reminder_id == "11111111-1111-1111-1111-111111111111"


def test_view_needs_no_fields():
    result = parse_tool_call("manage_reminder", '{"action": "view"}')
    assert isinstance(result, ParsedToolCall)
    assert result.is_high_risk is False


def test_extra_field_is_rejected():
    result = parse_tool_call("manage_reminder", '{"action": "view", "raw_text": "帮我看看提醒"}')
    assert isinstance(result, ToolCallError)
    assert result.reason == "schema_error"
    assert "raw_text" in result.fields
