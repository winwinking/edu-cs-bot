"""领域模型的 ORM 定义。所有业务表都带 tenant_id，查询必须带上这个过滤条件（硬性规则：租户隔离）。"""
import enum
import uuid
from datetime import datetime
from typing import Any, Optional

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
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
    # 转人工"坐席不在线"话术里展示的服务时间（阶段二 2.11），只是展示文案，不参与
    # 在线/不在线的判断——那个判断完全来自 mock-platform 的 /agents/status
    service_hours: Mapped[str] = mapped_column(String(32), nullable=False)


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
    # 连续不满意计数：命中不满意关键词就 +1，其他消息清零，累计到 2 次触发转人工（阶段二 2.11）
    dissatisfied_count: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)


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
    # 只有 assistant 回复会填这两个字段：intent 是本轮路由到的意图，meta 是 reply_end 里发给客户端的调试信息
    intent: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    meta: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)


class PendingActionStatus(str, enum.Enum):
    pending = "pending"
    executing = "executing"
    executed = "executed"
    failed = "failed"
    cancelled = "cancelled"
    expired = "expired"


class HandoffTrigger(str, enum.Enum):
    keyword = "keyword"
    dissatisfied = "dissatisfied"
    llm = "llm"


class HandoffStatus(str, enum.Enum):
    queued = "queued"
    left_message = "left_message"


class FollowupStatus(str, enum.Enum):
    open = "open"
    done = "done"


class KnowledgeDocument(Base):
    __tablename__ = "knowledge_documents"

    # 没有单独的 id：doc_id 是文件名衍生的业务主键，(tenant_id, doc_id) 天然唯一，不用再加一层代理键
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    doc_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    # 用来判断文件内容变没变，没变就跳过重新计算 embedding（阶段二 2.2 增量重建索引）
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "doc_id"], ["knowledge_documents.tenant_id", "knowledge_documents.doc_id"]
        ),
        UniqueConstraint("tenant_id", "doc_id", "clause_no", name="uq_knowledge_chunks_tenant_doc_clause"),
        Index("ix_knowledge_chunks_tenant_id", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    doc_id: Mapped[str] = mapped_column(String(128), nullable=False)
    doc_title: Mapped[str] = mapped_column(String(255), nullable=False)
    chapter: Mapped[str] = mapped_column(String(255), nullable=False)
    clause_no: Mapped[str] = mapped_column(String(32), nullable=False)
    clause_title: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 512 维哈希向量（阶段二 1.5），检索用 pgvector 的 <=> 余弦距离算子
    embedding: Mapped[list[float]] = mapped_column(Vector(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class GuardianLink(Base):
    __tablename__ = "guardian_links"

    # 三者做主键：一个家长可以关联多个学员，一个学员理论上也可能有多个家长账号
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    parent_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    student_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)


class PendingAction(Base):
    __tablename__ = "pending_actions"
    __table_args__ = (Index("ix_pending_actions_tenant_conversation", "tenant_id", "conversation_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id"), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    # 已经过 Pydantic 校验的参数，不是 LLM 原始输出——保证重新执行时不用再信任一次 LLM
    args: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    confirm_text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[PendingActionStatus] = mapped_column(
        Enum(PendingActionStatus, name="pending_action_status", native_enum=True), nullable=False
    )
    # 唯一约束：同一个待确认操作只调用一次下游平台接口，重试也不会重复执行
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    result: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_tenant_conversation_created", "tenant_id", "conversation_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    actor_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    # 复用 users.role 的同一个数据库枚举类型（user_role），角色取值只有那几个，没必要再建一套
    actor_role: Mapped[UserRole] = mapped_column(Enum(UserRole, name="user_role", native_enum=True), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_user_id: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id"), nullable=True)
    resource: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    result: Mapped[str] = mapped_column(String(32), nullable=False)
    trace_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    conversation_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("conversations.id"), nullable=True
    )
    # 应用代码只插入不更新不删除；写进去之前必须已经脱敏（硬性规则：日志/审计不留敏感信息原文）
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class HandoffTicket(Base):
    __tablename__ = "handoff_tickets"
    __table_args__ = (Index("ix_handoff_tickets_tenant_id", "tenant_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id"), nullable=False)
    trigger: Mapped[HandoffTrigger] = mapped_column(
        Enum(HandoffTrigger, name="handoff_trigger", native_enum=True), nullable=False
    )
    intent: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    attempted_actions: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    risk_flags: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[HandoffStatus] = mapped_column(
        Enum(HandoffStatus, name="handoff_ticket_status", native_enum=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class FollowupTask(Base):
    __tablename__ = "followup_tasks"
    __table_args__ = (Index("ix_followup_tasks_tenant_id", "tenant_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    query: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[FollowupStatus] = mapped_column(
        Enum(FollowupStatus, name="followup_task_status", native_enum=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
