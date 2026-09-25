"""conversation_summaries: 会话历史摘要（PHASE3.md 第 3 步）

Revision ID: 202609250002
Revises: 202609250001
Create Date: 2026-09-25

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "202609250002"
down_revision: Union[str, None] = "202609250001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "conversation_summaries",
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("conversations.id"), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("covered_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_table("conversation_summaries")
