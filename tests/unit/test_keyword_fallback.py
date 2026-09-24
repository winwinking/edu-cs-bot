"""覆盖 PHASE2.md 1.1：LLM 调用失败时降级为 worker 自己的关键词规则。"""
from app.worker.graph.classify import _keyword_fallback_classify, _strip_punctuation


def test_handoff_keyword_wins():
    result = _keyword_fallback_classify("我要转人工")
    assert result["intent"] == "handoff"
    assert result["route_source"] == "rule_fallback"


def test_sensitive_keyword_wins_and_flags_risk():
    result = _keyword_fallback_classify("帮我注销账号")
    assert result["intent"] == "high_risk"
    assert result["risk_flags"] == ["sensitive_request"]


def test_finance_keyword_with_trigger_word():
    result = _keyword_fallback_classify("我上个月的发票开了吗")
    assert result["intent"] == "finance_query"


def test_reminder_keyword():
    result = _keyword_fallback_classify("帮我设置一个提醒")
    assert result["intent"] == "reminder"


def test_platform_keyword():
    result = _keyword_fallback_classify("帮我打开课程表")
    assert result["intent"] == "platform_command"


def test_question_feature_falls_back_to_knowledge_qa():
    result = _keyword_fallback_classify("寒假班请假会退课时费吗")
    assert result["intent"] == "knowledge_qa"


def test_nothing_matches_uses_llm_unavailable_reason():
    result = _keyword_fallback_classify("你好")
    assert result["intent"] == "fallback"
    assert result["fallback_reason"] == "llm_unavailable"


def test_strip_punctuation_removes_common_marks():
    assert _strip_punctuation("确认关闭，谢谢！") == "确认关闭谢谢"
