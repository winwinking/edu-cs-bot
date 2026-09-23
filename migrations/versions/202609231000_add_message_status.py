"""add status to messages: 区分"已入库但没回复"和"已完整回复"，修复 worker 中途崩溃导致
用户消息被误判为重复而永远收不到回复的问题

Revision ID: 202609231000
Revises: 202609230001
Create Date: 2026-09-23

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "202609231000"
down_revision: Union[str, None] = "202609230001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # create_type=False：类型自己手动建，不让 add_column 时又自动建一次（会报重复类型）
    message_status = postgresql.ENUM("received", "replied", name="message_status", create_type=False)
    message_status.create(op.get_bind(), checkfirst=True)
    op.add_column("messages", sa.Column("status", message_status, nullable=True))
    # 历史数据（如果有）里的 user 消息，宽松地按"已回复"处理，不强行触发重新生成回复
    op.execute("UPDATE messages SET status = 'replied' WHERE role = 'user' AND status IS NULL")


def downgrade() -> None:
    op.drop_column("messages", "status")
    postgresql.ENUM(name="message_status").drop(op.get_bind(), checkfirst=True)
