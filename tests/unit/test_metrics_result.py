"""覆盖 worker_messages_total 的 result 标签映射：阶段三错误率统计、阶段四压测报告靠这个标签，
取值必须还是阶段一定下来的那几个（ok/llm_degraded/...），不能被 intent 顶替掉。"""
from app.worker.handler import _metric_result


def test_rule_route_is_ok():
    assert _metric_result("rule") == "ok"


def test_llm_route_is_ok():
    assert _metric_result("llm") == "ok"


def test_rule_fallback_route_is_llm_degraded():
    # LLM 调用本身失败、降级为关键词兜底，不管关键词最后判没判出意图，都算"降级"
    assert _metric_result("rule_fallback") == "llm_degraded"


def test_none_route_defaults_to_ok():
    assert _metric_result(None) == "ok"
