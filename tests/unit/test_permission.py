"""覆盖 PHASE2.md 2.6 的权限矩阵：学生查自己/查别人、家长查关联学员/非关联学员、坐席查、
管理员查、跨租户。对应 1.6 的规则。"""
from app.common.models import UserRole
from app.common.permissions import FinanceActor, can_access_finance


def test_student_can_query_self():
    actor = FinanceActor(tenant_id="t_a", user_id="u_a_1001", role=UserRole.student)
    assert can_access_finance(actor, "t_a", "u_a_1001") is True


def test_student_cannot_query_other_student():
    actor = FinanceActor(tenant_id="t_a", user_id="u_a_1004", role=UserRole.student)
    assert can_access_finance(actor, "t_a", "u_a_1001") is False


def test_parent_can_query_self():
    actor = FinanceActor(
        tenant_id="t_a", user_id="u_a_1002", role=UserRole.parent, linked_student_ids=frozenset({"u_a_1001"})
    )
    assert can_access_finance(actor, "t_a", "u_a_1002") is True


def test_parent_can_query_linked_student():
    actor = FinanceActor(
        tenant_id="t_a", user_id="u_a_1002", role=UserRole.parent, linked_student_ids=frozenset({"u_a_1001"})
    )
    assert can_access_finance(actor, "t_a", "u_a_1001") is True


def test_parent_cannot_query_unlinked_student():
    actor = FinanceActor(
        tenant_id="t_a", user_id="u_a_1002", role=UserRole.parent, linked_student_ids=frozenset({"u_a_1001"})
    )
    assert can_access_finance(actor, "t_a", "u_a_1004") is False


def test_agent_cannot_query_anyone_through_bot():
    # 坐席用后台查，不通过机器人查——哪怕查的是自己
    actor = FinanceActor(tenant_id="t_a", user_id="u_a_1003", role=UserRole.agent)
    assert can_access_finance(actor, "t_a", "u_a_1001") is False
    assert can_access_finance(actor, "t_a", "u_a_1003") is False


def test_admin_cannot_query_anyone_through_bot():
    actor = FinanceActor(tenant_id="t_a", user_id="u_a_admin", role=UserRole.admin)
    assert can_access_finance(actor, "t_a", "u_a_1001") is False


def test_cross_tenant_is_always_rejected():
    # 即使 user_id 字符串"看起来像"关联，跨租户也一律拒绝
    student_actor = FinanceActor(tenant_id="t_a", user_id="u_a_1001", role=UserRole.student)
    assert can_access_finance(student_actor, "t_b", "u_a_1001") is False

    parent_actor = FinanceActor(
        tenant_id="t_a", user_id="u_a_1002", role=UserRole.parent, linked_student_ids=frozenset({"u_b_1001"})
    )
    assert can_access_finance(parent_actor, "t_b", "u_b_1001") is False
