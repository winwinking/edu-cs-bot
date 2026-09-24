"""tenant service hours: 转人工"不在线"话术里展示的服务时间改成从租户配置读取，不再写死在代码里

Revision ID: 202609241200
Revises: 202609240001
Create Date: 2026-09-24

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "202609241200"
down_revision: Union[str, None] = "202609240001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 先允许为空加进去，回填已有租户的数据后再收紧成 NOT NULL——两个种子租户已知取值，
    # 但迁移脚本本身不应该假设"只有这两个租户"，用固定的默认值兜底更安全
    op.add_column("tenants", sa.Column("service_hours", sa.String(length=32), nullable=True))
    op.execute("UPDATE tenants SET service_hours = '9:00 至 21:00' WHERE id = 't_a'")
    op.execute("UPDATE tenants SET service_hours = '8:30 至 20:30' WHERE id = 't_b'")
    op.execute("UPDATE tenants SET service_hours = '9:00 至 21:00' WHERE service_hours IS NULL")
    op.alter_column("tenants", "service_hours", nullable=False)


def downgrade() -> None:
    op.drop_column("tenants", "service_hours")
