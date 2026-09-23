"""领域模型的 ORM 定义。所有业务表都带 tenant_id，查询必须带上这个过滤条件（硬性规则：租户隔离）。"""
import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.common.db import Base


class UserRole(str, enum.Enum):
    student = "student"
    parent = "parent"
    agent = "agent"
    admin = "admin"


class MessageRole(str, enum.Enum):
    user = "user"
    assistant = "assistant"


class MessageStatus(str, enum.Enum):
    # 只用在 role=user 的消息上：区分"已入库但还没回复"和"已经完整回复过"，
    # 这样 worker 中途崩溃、消息被重新投递时，能分清是真重复还是没处理完，不会让用户收不到回复
    received = "received"
    replied = "replied"


class Tenant(Base):
    __tablename__ = "tenants"

    # 租户 id 用业务可读的字符串（如 t_a），不用自增数字，方便手工测试和日志排查
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (Index("ix_users_tenant_id", "tenant_id"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(Enum(UserRole, name="user_role", native_enum=True), nullable=False)
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (Index("ix_conversations_tenant_user", "tenant_id", "user_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        # 唯一约束是 worker 端幂等的最后一道防线：队列至少一次投递，同一条消息可能被投递两次，
        # 第二次 INSERT 会冲突——但冲突只代表"这条消息之前插过"，不代表"已经回复完了"，
        # 还要看 status 才能判断是真重复（replied）还是处理到一半崩了要重新处理（received）
        UniqueConstraint("tenant_id", "message_id", name="uq_messages_tenant_message_id"),
        Index("ix_messages_tenant_conversation_created", "tenant_id", "conversation_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id"), nullable=False)
    message_id: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[MessageRole] = mapped_column(Enum(MessageRole, name="message_role", native_enum=True), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    trace_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    # 只有 role=user 的行会设这个字段，assistant 行留空；见 MessageStatus 的注释
    status: Mapped[Optional[MessageStatus]] = mapped_column(
        Enum(MessageStatus, name="message_status", native_enum=True), nullable=True
    )
