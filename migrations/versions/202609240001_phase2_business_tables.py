"""phase2: knowledge base, guardian links, pending actions, audit logs, handoff, followup tasks

Revision ID: 202609240001
Revises: 202609231000
Create Date: 2026-09-24

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "202609240001"
down_revision: Union[str, None] = "202609231000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 复用阶段一已经建好的 user_role 枚举类型，不再新建一套（角色取值本来就是同一批）
    user_role = postgresql.ENUM("student", "parent", "agent", "admin", name="user_role", create_type=False)

    pending_action_status = postgresql.ENUM(
        "pending", "executing", "executed", "failed", "cancelled", "expired",
        name="pending_action_status", create_type=False,
    )
    pending_action_status.create(op.get_bind(), checkfirst=True)

    handoff_trigger = postgresql.ENUM("keyword", "dissatisfied", "llm", name="handoff_trigger", create_type=False)
    handoff_trigger.create(op.get_bind(), checkfirst=True)

    handoff_ticket_status = postgresql.ENUM(
        "queued", "left_message", name="handoff_ticket_status", create_type=False
    )
    handoff_ticket_status.create(op.get_bind(), checkfirst=True)

    followup_task_status = postgresql.ENUM("open", "done", name="followup_task_status", create_type=False)
    followup_task_status.create(op.get_bind(), checkfirst=True)

    # knowledge_documents 没有代理主键：doc_id 是文件名衍生的业务 id，(tenant_id, doc_id) 天然唯一
    op.create_table(
        "knowledge_documents",
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id"), primary_key=True),
        sa.Column("doc_id", sa.String(length=128), primary_key=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "knowledge_chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("doc_id", sa.String(length=128), nullable=False),
        sa.Column("doc_title", sa.String(length=255), nullable=False),
        sa.Column("chapter", sa.String(length=255), nullable=False),
        sa.Column("clause_no", sa.String(length=32), nullable=False),
        sa.Column("clause_title", sa.String(length=255), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(512), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "doc_id"], ["knowledge_documents.tenant_id", "knowledge_documents.doc_id"]
        ),
    )
    op.create_index("ix_knowledge_chunks_tenant_id", "knowledge_chunks", ["tenant_id"])
    op.create_unique_constraint(
        "uq_knowledge_chunks_tenant_doc_clause", "knowledge_chunks", ["tenant_id", "doc_id", "clause_no"]
    )

    op.create_table(
        "guardian_links",
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id"), primary_key=True),
        sa.Column("parent_user_id", sa.String(length=64), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("student_user_id", sa.String(length=64), sa.ForeignKey("users.id"), primary_key=True),
    )

    op.create_table(
        "pending_actions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("user_id", sa.String(length=64), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("conversations.id"), nullable=False),
        sa.Column("tool_name", sa.String(length=64), nullable=False),
        sa.Column("args", postgresql.JSONB(), nullable=False),
        sa.Column("confirm_text", sa.Text(), nullable=False),
        sa.Column("status", pending_action_status, nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_unique_constraint("uq_pending_actions_idempotency_key", "pending_actions", ["idempotency_key"])
    op.create_index(
        "ix_pending_actions_tenant_conversation", "pending_actions", ["tenant_id", "conversation_id"]
    )

    op.create_table(
        "audit_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("actor_user_id", sa.String(length=64), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("actor_role", user_role, nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target_user_id", sa.String(length=64), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("resource", sa.String(length=128), nullable=True),
        sa.Column("result", sa.String(length=32), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("conversations.id"), nullable=True),
        sa.Column("detail", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_audit_logs_tenant_conversation_created", "audit_logs", ["tenant_id", "conversation_id", "created_at"]
    )

    op.create_table(
        "handoff_tickets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("user_id", sa.String(length=64), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("conversations.id"), nullable=False),
        sa.Column("trigger", handoff_trigger, nullable=False),
        sa.Column("intent", sa.String(length=64), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("attempted_actions", postgresql.JSONB(), nullable=False),
        sa.Column("risk_flags", postgresql.JSONB(), nullable=False),
        sa.Column("status", handoff_ticket_status, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_handoff_tickets_tenant_id", "handoff_tickets", ["tenant_id"])

    op.create_table(
        "followup_tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("user_id", sa.String(length=64), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("conversations.id"), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("query", postgresql.JSONB(), nullable=False),
        sa.Column("status", followup_task_status, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_followup_tasks_tenant_id", "followup_tasks", ["tenant_id"])

    op.add_column(
        "conversations",
        sa.Column("dissatisfied_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column("messages", sa.Column("intent", sa.String(length=64), nullable=True))
    op.add_column("messages", sa.Column("meta", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "meta")
    op.drop_column("messages", "intent")
    op.drop_column("conversations", "dissatisfied_count")

    op.drop_index("ix_followup_tasks_tenant_id", table_name="followup_tasks")
    op.drop_table("followup_tasks")
    postgresql.ENUM(name="followup_task_status").drop(op.get_bind(), checkfirst=True)

    op.drop_index("ix_handoff_tickets_tenant_id", table_name="handoff_tickets")
    op.drop_table("handoff_tickets")
    postgresql.ENUM(name="handoff_ticket_status").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="handoff_trigger").drop(op.get_bind(), checkfirst=True)

    op.drop_index("ix_audit_logs_tenant_conversation_created", table_name="audit_logs")
    op.drop_table("audit_logs")

    op.drop_index("ix_pending_actions_tenant_conversation", table_name="pending_actions")
    op.drop_constraint("uq_pending_actions_idempotency_key", "pending_actions", type_="unique")
    op.drop_table("pending_actions")
    postgresql.ENUM(name="pending_action_status").drop(op.get_bind(), checkfirst=True)

    op.drop_table("guardian_links")

    op.drop_constraint("uq_knowledge_chunks_tenant_doc_clause", "knowledge_chunks", type_="unique")
    op.drop_index("ix_knowledge_chunks_tenant_id", table_name="knowledge_chunks")
    op.drop_table("knowledge_chunks")

    op.drop_table("knowledge_documents")
