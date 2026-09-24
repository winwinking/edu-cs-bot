"""覆盖 PHASE2.md 2.9 第 4 点第 1 条：确定被查的人。"""
from app.common.models import UserRole
from app.worker.graph.finance import _resolve_target_user_id


def test_explicit_target_user_id_wins():
    assert _resolve_target_user_id("u_a_1002", UserRole.parent, "u_a_1001", frozenset({"u_a_1001"})) == "u_a_1001"


def test_student_without_target_queries_self():
    assert _resolve_target_user_id("u_a_1001", UserRole.student, None, frozenset()) == "u_a_1001"


def test_parent_without_target_and_one_linked_student_queries_that_student():
    assert (
        _resolve_target_user_id("u_a_1002", UserRole.parent, None, frozenset({"u_a_1001"})) == "u_a_1001"
    )


def test_parent_without_target_and_multiple_linked_students_defaults_to_self():
    # 关联了不止一个学员又没指定查谁，不能瞎猜，退回查自己（自己大概率查不到什么，
    # 但至少不会查错人；2.10 的多轮澄清会解决"该问哪个学员"这个问题）
    result = _resolve_target_user_id("u_a_1002", UserRole.parent, None, frozenset({"u_a_1001", "u_a_9999"}))
    assert result == "u_a_1002"


def test_agent_without_target_queries_self():
    assert _resolve_target_user_id("u_a_1003", UserRole.agent, None, frozenset()) == "u_a_1003"
