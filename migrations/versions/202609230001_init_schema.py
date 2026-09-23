"""init schema: tenants, users, conversations, messages

Revision ID: 202609230001
Revises:
Create Date: 2026-09-23

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "202609230001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 阶段二要用 pgvector 存 embedding，这里先启用扩展，表结构留到阶段二再加
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "tenants",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
    )

    # create_type=False：类型我们自己手动建，不让 create_table 在建表时又自动建一次（会报重复类型）
    user_role = postgresql.ENUM("student", "parent", "agent", "admin", name="user_role", create_type=False)
    user_role.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "users",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("role", user_role, nullable=False),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_users_tenant_id", "users", ["tenant_id"])

    op.create_table(
        "conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("user_id", sa.String(length=64), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_conversations_tenant_user", "conversations", ["tenant_id", "user_id"])

    message_role = postgresql.ENUM("user", "assistant", name="message_role", create_type=False)
    message_role.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column(
            "conversation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("conversations.id"), nullable=False
        ),
        sa.Column("message_id", sa.String(length=64), nullable=False),
        sa.Column("role", message_role, nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    # 唯一约束是 worker 端幂等的最后一道防线：队列至少一次投递 + 这个约束 = 业务只处理一次
    op.create_unique_constraint("uq_messages_tenant_message_id", "messages", ["tenant_id", "message_id"])
    op.create_index(
        "ix_messages_tenant_conversation_created",
        "messages",
        ["tenant_id", "conversation_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_messages_tenant_conversation_created", table_name="messages")
    op.drop_constraint("uq_messages_tenant_message_id", "messages", type_="unique")
    op.drop_table("messages")
    postgresql.ENUM(name="message_role").drop(op.get_bind(), checkfirst=True)

    op.drop_index("ix_conversations_tenant_user", table_name="conversations")
    op.drop_table("conversations")

    op.drop_index("ix_users_tenant_id", table_name="users")
    op.drop_table("users")
    postgresql.ENUM(name="user_role").drop(op.get_bind(), checkfirst=True)

    op.drop_table("tenants")
    # 不 drop vector 扩展，可能还有别的 schema 在用
