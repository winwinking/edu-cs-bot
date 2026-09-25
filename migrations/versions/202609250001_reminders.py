"""reminders: 提醒表 + tenants.timezone（PHASE3.md 第 1 步）

Revision ID: 202609250001
Revises: 202609241200
Create Date: 2026-09-25

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "202609250001"
down_revision: Union[str, None] = "202609241200"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

reminder_repeat = postgresql.ENUM(
    "none", "daily", "weekly", "workdays", name="reminder_repeat", create_type=False
)
reminder_status = postgresql.ENUM("active", "cancelled", "done", name="reminder_status", create_type=False)


def upgrade() -> None:
    # tenants.timezone：默认 Asia/Shanghai，直接给 server_default，新老租户都不会是 NULL，
    # 不需要像 service_hours 那样先允许为空再回填（这一列本来就有一个通用的合理默认值）
    op.add_column(
        "tenants",
        sa.Column("timezone", sa.String(length=64), nullable=False, server_default="Asia/Shanghai"),
    )

    reminder_repeat.create(op.get_bind())
    reminder_status.create(op.get_bind())

    op.create_table(
        "reminders",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("user_id", sa.String(length=64), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("conversations.id"), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("event_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("repeat", reminder_repeat, nullable=False),
        sa.Column("advance_minutes", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("next_trigger_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", reminder_status, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_reminders_tenant_user", "reminders", ["tenant_id", "user_id"])
    # 这个复合索引直接对应 scheduler 每秒的查询模式：WHERE status='active' AND next_trigger_at<=now()
    op.create_index("ix_reminders_status_next_trigger", "reminders", ["status", "next_trigger_at"])


def downgrade() -> None:
    op.drop_index("ix_reminders_status_next_trigger", table_name="reminders")
    op.drop_index("ix_reminders_tenant_user", table_name="reminders")
    op.drop_table("reminders")
    reminder_status.drop(op.get_bind())
    reminder_repeat.drop(op.get_bind())
    op.drop_column("tenants", "timezone")
