"""覆盖 mock-llm 新增的问候规则（插在 R4 之前，见 PHASE2.md 2.7 后续调整）。

mocks/ 目录只打进 mocks 镜像，不在 tools/app 镜像里（两边刻意不共享代码，见 mocks 独立性设计）；
这个文件在 tools 镜像里跑的时候 import 不到 mocks 包，用 importorskip 优雅跳过，不让整个
tests/unit 目录在 tools 镜像里跑不起来。真正执行要用 mocks 镜像：
    docker compose run --rm mock-llm pytest -q tests/unit/test_mock_llm_rules.py
"""
import pytest

pytest.importorskip("mocks.mock_llm.rules")

from mocks.mock_llm.rules import match_tool_call  # noqa: E402


def test_greeting_with_question_char_is_not_treated_as_knowledge_qa():
    # "在吗"里的"吗"字本来会被 R4 命中成 search_knowledge，问候规则要先把它拦下来
    assert match_tool_call("你好，在吗") is None


def test_plain_greetings_do_not_trigger_any_tool():
    for text in ("你好", "您好", "在吗", "在不在", "hi", "HELLO", "Hi！"):
        assert match_tool_call(text) is None


def test_greeting_words_combined_still_counts_as_greeting():
    assert match_tool_call("你好，您好，在吗") is None


def test_non_greeting_question_still_routes_to_knowledge():
    result = match_tool_call("寒假班请假会退课时费吗")
    assert result is not None
    assert result[0] == "search_knowledge"


def test_finance_rule_still_takes_priority_over_greeting_check():
    result = match_tool_call("我上个月的发票开了吗")
    assert result == ("query_finance", {"kind": "invoices", "period": "last_month"})


def test_platform_command_rule_unaffected():
    result = match_tool_call("帮我把自动续费关了")
    assert result == ("platform_command", {"action": "disable_auto_renew"})


def test_greeting_mixed_with_other_words_is_not_a_pure_greeting():
    # 不是"只由问候词组成"，应该继续走后面的规则（这句没有问句特征词，R4 也不命中，落到 R5）
    result = match_tool_call("你好，我想问一下")
    assert result is None  # 不是因为命中问候规则，是 R1~R4 都不命中，R5 兜底也返回 None
