"""覆盖 PHASE2.md 2.10 里不用数据库/mock-platform 就能测的纯逻辑：怎么从订阅列表里选出要
切换哪门课、确认话术/取消话术/失败话术怎么按 action 拼。跟数据库、mock-platform 交互的部分
（原子抢占、幂等、审计）走 2.10 验证脚本里的真实端到端对话，不在这里用假 session 模拟。
"""
from app.worker.graph.command import (
    _build_confirm_text,
    _build_failure_reply,
    _build_success_reply,
    _cancel_state_text,
    _confirm_phrase,
    _format_date_cn,
    _resolve_toggle_course,
)

_SUBS_ONE_ON = [
    {"course_name": "春季数学班", "auto_renew": True},
    {"course_name": "口语提高班", "auto_renew": False},
]


def test_resolve_toggle_course_picks_the_only_course_on():
    outcome, payload = _resolve_toggle_course(_SUBS_ONE_ON, None, want_auto_renew=False)
    assert (outcome, payload) == ("ok", "春季数学班")


def test_resolve_toggle_course_no_course_currently_on():
    subs = [{"course_name": "口语提高班", "auto_renew": False}]
    outcome, payload = _resolve_toggle_course(subs, None, want_auto_renew=False)
    assert outcome == "none"
    assert payload is None


def test_resolve_toggle_course_multiple_on_needs_clarification():
    subs = [
        {"course_name": "春季数学班", "auto_renew": True},
        {"course_name": "春季英语班", "auto_renew": True},
    ]
    outcome, payload = _resolve_toggle_course(subs, None, want_auto_renew=False)
    assert outcome == "ambiguous"
    assert set(payload) == {"春季数学班", "春季英语班"}


def test_resolve_toggle_course_named_course_already_target_state():
    outcome, payload = _resolve_toggle_course(_SUBS_ONE_ON, "口语提高班", want_auto_renew=False)
    assert (outcome, payload) == ("already", "口语提高班")


def test_resolve_toggle_course_named_course_not_in_subscriptions():
    outcome, payload = _resolve_toggle_course(_SUBS_ONE_ON, "寒假班", want_auto_renew=False)
    assert (outcome, payload) == ("not_found", None)


def test_resolve_toggle_course_named_course_matches_and_switchable():
    outcome, payload = _resolve_toggle_course(_SUBS_ONE_ON, "春季数学班", want_auto_renew=False)
    assert (outcome, payload) == ("ok", "春季数学班")


def test_confirm_text_matches_spec_example_for_disable_auto_renew():
    text = _build_confirm_text("disable_auto_renew", {"course_name": "春季数学班"})
    assert text == (
        "我先确认一下：你要关闭的是“春季数学班”的自动续费，对吗？"
        "关闭后不影响已购课程，本月已排课程照常上。回复“确认关闭”我就处理。"
    )


def test_success_reply_matches_spec_example_for_disable_auto_renew():
    text = _build_success_reply("disable_auto_renew", {"course_name": "春季数学班"})
    assert text == "已关闭“春季数学班”的自动续费。本月已排课程照常上，下一期不会再自动扣款，需要重新开通随时告诉我。"


def test_failure_reply_matches_spec_example_for_disable_auto_renew():
    text = _build_failure_reply("disable_auto_renew")
    assert text == "这次没有关闭成功，我已记录。你可以稍后再试，或者回复“转人工”。"


def test_confirm_phrase_by_action():
    assert _confirm_phrase("disable_auto_renew") == "确认关闭"
    assert _confirm_phrase("enable_auto_renew") == "确认开通"
    assert _confirm_phrase("submit_leave") == "确认提交"


def test_cancel_state_text_by_action():
    assert _cancel_state_text("disable_auto_renew", {}) == "自动续费保持开启"
    assert _cancel_state_text("enable_auto_renew", {}) == "自动续费保持关闭"
    assert _cancel_state_text("submit_leave", {}) == "请假没有提交，课程照常安排"


def test_leave_confirm_text_without_course_name_still_readable():
    text = _build_confirm_text("submit_leave", {"course_name": None, "date": "2026-09-25"})
    assert "9月25日" in text
    assert "确认提交" in text


def test_format_date_cn():
    assert _format_date_cn("2026-01-05") == "1月5日"
    assert _format_date_cn(None) == ""
