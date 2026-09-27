"""覆盖 PHASE4.md 4.6 人审确认第 1 条：提醒创建/修改/取消要有结构化日志，只记动作、结果和
reminder_id，不记标题/时间这类消息相关内容。

不连真实数据库，`load_active_reminders`/`_resolve_target_reminder` 直接 monkeypatch 掉
（这两个各自已经有别的测试覆盖），这里只关心命中之后 `_handle_create`/`_handle_update_or_cancel`
自己的日志行为；`session` 只需要支持 `add()`/`commit()`。
"""
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from structlog.testing import capture_logs

from app.common.models import ReminderStatus
from app.worker.graph import reminder as reminder_module


class _FakeSession:
    def __init__(self):
        self.added: list = []
        self.commit_count = 0
        self.rollback_count = 0

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commit_count += 1

    async def rollback(self):
        self.rollback_count += 1


@pytest.mark.asyncio
async def test_handle_create_logs_reminder_id_only():
    session = _FakeSession()
    conversation_id = str(uuid.uuid4())
    args = {"title": "交作业", "event_time": "2026-09-30 09:00", "repeat": None, "advance_minutes": None}

    with capture_logs() as logs:
        result = await reminder_module._handle_create(
            session, "t_a", "u_a_1001", conversation_id, "Asia/Shanghai", args
        )

    entries = [e for e in logs if e.get("event") == "创建提醒"]
    assert len(entries) == 1
    assert entries[0]["conversation_id"] == conversation_id
    assert entries[0]["user_id"] == "u_a_1001"
    assert entries[0]["status"] == "ok"
    created_reminder = session.added[0]
    assert entries[0]["reminder_id"] == str(created_reminder.id)
    # 只记 ID，不记标题这类消息内容
    assert "交作业" not in str(entries[0])
    assert result["tools_meta"] == [{"name": "manage_reminder", "status": "ok"}]


def _fake_reminder(**overrides) -> SimpleNamespace:
    base = dict(
        id=uuid.uuid4(),
        title="交作业",
        status=ReminderStatus.active,
        event_at=datetime.now(timezone.utc) + timedelta(days=1),
        timezone="Asia/Shanghai",
        advance_minutes=30,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_handle_update_or_cancel_logs_cancel(monkeypatch):
    target = _fake_reminder()

    async def fake_load_active(session, tenant_id, user_id):
        return [target]

    async def fake_resolve(session, tenant_id, user_id, reminder_id_arg, active_reminders, tenant_timezone):
        return target, None

    monkeypatch.setattr(reminder_module, "load_active_reminders", fake_load_active)
    monkeypatch.setattr(reminder_module, "_resolve_target_reminder", fake_resolve)

    session = _FakeSession()
    conversation_id = str(uuid.uuid4())

    with capture_logs() as logs:
        await reminder_module._handle_update_or_cancel(
            session, "t_a", "u_a_1001", conversation_id, "Asia/Shanghai", "cancel", {}
        )

    entries = [e for e in logs if e.get("event") == "取消提醒"]
    assert len(entries) == 1
    assert entries[0]["conversation_id"] == conversation_id
    assert entries[0]["user_id"] == "u_a_1001"
    assert entries[0]["reminder_id"] == str(target.id)
    assert entries[0]["status"] == "ok"
    assert target.status == ReminderStatus.cancelled


@pytest.mark.asyncio
async def test_handle_update_or_cancel_logs_update(monkeypatch):
    target = _fake_reminder()

    async def fake_load_active(session, tenant_id, user_id):
        return [target]

    async def fake_resolve(session, tenant_id, user_id, reminder_id_arg, active_reminders, tenant_timezone):
        return target, None

    monkeypatch.setattr(reminder_module, "load_active_reminders", fake_load_active)
    monkeypatch.setattr(reminder_module, "_resolve_target_reminder", fake_resolve)

    session = _FakeSession()
    conversation_id = str(uuid.uuid4())
    args = {"title": "改成交周报"}

    with capture_logs() as logs:
        await reminder_module._handle_update_or_cancel(
            session, "t_a", "u_a_1001", conversation_id, "Asia/Shanghai", "update", args
        )

    entries = [e for e in logs if e.get("event") == "修改提醒"]
    assert len(entries) == 1
    assert entries[0]["reminder_id"] == str(target.id)
    assert entries[0]["status"] == "ok"
    # 只记 ID，不记改成了什么标题
    assert "改成交周报" not in str(entries[0])


@pytest.mark.asyncio
async def test_handle_update_or_cancel_does_not_log_on_early_return(monkeypatch):
    """没有匹配到任何提醒（比如没有生效中的提醒）时，_resolve_target_reminder 直接返回
    早退结果，不应该记创建/修改/取消这几条日志——这条路径本来就没有真的操作到任何提醒。"""

    async def fake_load_active(session, tenant_id, user_id):
        return []

    async def fake_resolve(session, tenant_id, user_id, reminder_id_arg, active_reminders, tenant_timezone):
        return None, {"reply_plan": {"mode": "template", "text": "你目前没有生效中的提醒"}}

    monkeypatch.setattr(reminder_module, "load_active_reminders", fake_load_active)
    monkeypatch.setattr(reminder_module, "_resolve_target_reminder", fake_resolve)

    session = _FakeSession()

    with capture_logs() as logs:
        await reminder_module._handle_update_or_cancel(
            session, "t_a", "u_a_1001", str(uuid.uuid4()), "Asia/Shanghai", "cancel", {}
        )

    assert not [e for e in logs if e.get("event") in ("取消提醒", "修改提醒")]
