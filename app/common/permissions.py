"""财务查询权限判断（PHASE2.md 1.6）。

规则：学生只能查自己；家长能查自己和关联学员；坐席和管理员不能通过机器人查财务（他们用后台，
最小权限）；跨租户一律拒绝。

actor 的身份（tenant_id/user_id）只能来自 gateway 从 JWT 解析出的值，角色和家长关联关系
只能来自数据库查询结果——任何地方都不能用 LLM 输出里的身份字段（CLAUDE.md 硬性规则）。
"""
from dataclasses import dataclass, field

from app.common.models import UserRole


@dataclass(frozen=True)
class FinanceActor:
    tenant_id: str
    user_id: str
    role: UserRole
    # 只有 role=parent 时有意义：worker 从 guardian_links 表查出来的关联学员 id 集合
    linked_student_ids: frozenset[str] = field(default_factory=frozenset)


def can_access_finance(actor: FinanceActor, target_tenant_id: str, target_user_id: str) -> bool:
    if actor.tenant_id != target_tenant_id:
        return False
    if actor.role == UserRole.student:
        return target_user_id == actor.user_id
    if actor.role == UserRole.parent:
        return target_user_id == actor.user_id or target_user_id in actor.linked_student_ids
    # agent / admin：坐席和管理员不能通过机器人查财务，最小权限
    return False
