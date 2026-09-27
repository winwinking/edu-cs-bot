"""覆盖 PHASE4.md 4.6 人审确认第 1 条：高风险指令发起二次确认/用户确认/用户取消/指令执行结果
这几个关键动作要有结构化日志，只记动作、结果和 ID，不记参数/回复原文。

不连真实数据库/mock-platform，用本文件手写的假 session 模拟 `session.execute()` 依次返回的
结果（confirm_action/cancel_action 内部按顺序发起好几次 execute，用一个队列模拟），
`_find_target_pending_action` 直接 monkeypatch 掉——它本身已经在其它地方测过，这里只关心
拿到目标之后 confirm_action/cancel_action 自己的日志行为。
"""
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from structlog.testing import capture_logs

from app.common.models import PendingActionStatus
from app.worker.graph import command as command_module


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeSession:
    def __init__(self, execute_results=None, get_result=None):
        self._results = list(execute_results or [])
        self._get_result = get_result
        self.added: list = []
        self.commit_count = 0

    async def execute(self, stmt):
        return self._results.pop(0)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commit_count += 1

    async def get(self, model, pk):
        return self._get_result


def _runtime(session) -> SimpleNamespace:
    return SimpleNamespace(context=SimpleNamespace(session=session))


def _state(**overrides) -> dict:
    base = {
        "tenant_id": "t_a",
        "user_id": "u_a_1001",
        "conversation_id": str(uuid.uuid4()),
        "role": "student",
    }
    base.update(overrides)
    return base


# ---------- command()：低风险指令直接执行 ----------


@pytest.mark.asyncio
async def test_command_logs_execution_result_on_success(monkeypatch):
    async def fake_submit_command(**kwargs):
        return {"status": "success"}

    monkeypatch.setattr(command_module, "submit_command", fake_submit_command)

    session = _FakeSession()
    state = _state(
        tool_call={"name": "platform_command", "args": {"action": "open_schedule"}},
        message_id="msg-1",
    )

    with capture_logs() as logs:
        await command_module.command(state, _runtime(session))

    entries = [e for e in logs if e.get("event") == "平台指令执行结果"]
    assert len(entries) == 1
    assert entries[0]["conversation_id"] == state["conversation_id"]
    assert entries[0]["user_id"] == "u_a_1001"
    assert entries[0]["action"] == "open_schedule"
    assert entries[0]["status"] == "ok"


@pytest.mark.asyncio
async def test_command_logs_upstream_error_status(monkeypatch):
    from app.common.platform_client import PlatformUnavailable

    async def fake_submit_command(**kwargs):
        raise PlatformUnavailable("mock-platform 超时")

    monkeypatch.setattr(command_module, "submit_command", fake_submit_command)

    session = _FakeSession()
    state = _state(
        tool_call={"name": "platform_command", "args": {"action": "query_study_report"}},
        message_id="msg-2",
    )

    with capture_logs() as logs:
        await command_module.command(state, _runtime(session))

    entries = [e for e in logs if e.get("event") == "平台指令执行结果"]
    assert entries[0]["status"] == "upstream_error"


# ---------- request_confirmation()：发起二次确认 ----------


@pytest.mark.asyncio
async def test_request_confirmation_logs_new_pending_action(monkeypatch):
    async def fake_find_target(session, tenant_id, conversation_id):
        return None

    monkeypatch.setattr(command_module, "_find_target_pending_action", fake_find_target)

    session = _FakeSession()
    state = _state(
        tool_call={
            "name": "platform_command",
            "args": {"action": "submit_leave", "course_name": "寒假班", "date": "2026-09-30", "reason": None},
        },
    )

    with capture_logs() as logs:
        result = await command_module.request_confirmation(state, _runtime(session))

    entries = [e for e in logs if e.get("event") == "高风险指令发起二次确认"]
    assert len(entries) == 1
    assert entries[0]["conversation_id"] == state["conversation_id"]
    assert entries[0]["user_id"] == "u_a_1001"
    assert entries[0]["action"] == "submit_leave"
    assert entries[0]["pending_action_id"] == result["pending_action_id"]


# ---------- confirm_action()：用户确认 ----------


def _pending_target(action="disable_auto_renew", status=PendingActionStatus.pending, expired=False):
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=uuid.uuid4(),
        status=status,
        expires_at=now - timedelta(seconds=1) if expired else now + timedelta(seconds=300),
        args={"action": action, "course_name": "春季数学班"},
        user_id="u_a_1001",
        idempotency_key="key-1",
    )


@pytest.mark.asyncio
async def test_confirm_action_logs_no_pending_when_nothing_found(monkeypatch):
    async def fake_find_target(session, tenant_id, conversation_id):
        return None

    monkeypatch.setattr(command_module, "_find_target_pending_action", fake_find_target)
    session = _FakeSession()

    with capture_logs() as logs:
        await command_module.confirm_action(_state(), _runtime(session))

    entries = [e for e in logs if e.get("event") == "用户确认高风险指令"]
    assert len(entries) == 1
    assert entries[0]["outcome"] == "no_pending"
    assert entries[0]["pending_action_id"] is None


@pytest.mark.asyncio
async def test_confirm_action_logs_acquired_and_execution_result(monkeypatch):
    target = _pending_target()

    async def fake_find_target(session, tenant_id, conversation_id):
        return target

    async def fake_submit_command(**kwargs):
        return {"status": "success"}

    monkeypatch.setattr(command_module, "_find_target_pending_action", fake_find_target)
    monkeypatch.setattr(command_module, "submit_command", fake_submit_command)

    # 顺序：acquire_stmt -> 抢到（返回 target 本身模拟 acquired）；执行成功后还有一次
    # update(...).values(status=...) 的 execute，返回值不会被用到
    session = _FakeSession(execute_results=[_Result(target), _Result(None)])

    with capture_logs() as logs:
        await command_module.confirm_action(_state(), _runtime(session))

    confirm_entries = [e for e in logs if e.get("event") == "用户确认高风险指令"]
    assert confirm_entries[0]["outcome"] == "acquired"
    assert confirm_entries[0]["pending_action_id"] == str(target.id)

    result_entries = [e for e in logs if e.get("event") == "平台指令执行结果"]
    assert result_entries[0]["status"] == "ok"
    assert result_entries[0]["action"] == "disable_auto_renew"


@pytest.mark.asyncio
async def test_confirm_action_logs_expired_when_acquire_loses_to_expiry(monkeypatch):
    target = _pending_target(expired=True)

    async def fake_find_target(session, tenant_id, conversation_id):
        return target

    monkeypatch.setattr(command_module, "_find_target_pending_action", fake_find_target)

    # acquire_stmt 抢不到（返回 None）；session.get() 查到状态还是 pending -> 判定为过期，
    # 再执行一次 update 标记 expired（返回值不用）
    session = _FakeSession(
        execute_results=[_Result(None), _Result(None)],
        get_result=SimpleNamespace(id=target.id, status=PendingActionStatus.pending),
    )

    with capture_logs() as logs:
        await command_module.confirm_action(_state(), _runtime(session))

    entries = [e for e in logs if e.get("event") == "用户确认高风险指令"]
    assert entries[0]["outcome"] == "expired"
    assert entries[0]["pending_action_id"] == str(target.id)


@pytest.mark.asyncio
async def test_confirm_action_logs_already_processed(monkeypatch):
    target = _pending_target()

    async def fake_find_target(session, tenant_id, conversation_id):
        return target

    monkeypatch.setattr(command_module, "_find_target_pending_action", fake_find_target)

    session = _FakeSession(
        execute_results=[_Result(None)],
        get_result=SimpleNamespace(status=PendingActionStatus.executed),
    )

    with capture_logs() as logs:
        await command_module.confirm_action(_state(), _runtime(session))

    entries = [e for e in logs if e.get("event") == "用户确认高风险指令"]
    assert entries[0]["outcome"] == "already_processed"


# ---------- cancel_action()：用户取消 ----------


@pytest.mark.asyncio
async def test_cancel_action_logs_no_pending(monkeypatch):
    async def fake_find_target(session, tenant_id, conversation_id):
        return None

    monkeypatch.setattr(command_module, "_find_target_pending_action", fake_find_target)
    session = _FakeSession()

    with capture_logs() as logs:
        await command_module.cancel_action(_state(), _runtime(session))

    entries = [e for e in logs if e.get("event") == "用户取消高风险指令"]
    assert entries[0]["outcome"] == "no_pending"
    assert entries[0]["pending_action_id"] is None


@pytest.mark.asyncio
async def test_cancel_action_logs_cancelled(monkeypatch):
    target = _pending_target()

    async def fake_find_target(session, tenant_id, conversation_id):
        return target

    monkeypatch.setattr(command_module, "_find_target_pending_action", fake_find_target)
    session = _FakeSession(execute_results=[_Result(target)])

    with capture_logs() as logs:
        await command_module.cancel_action(_state(), _runtime(session))

    entries = [e for e in logs if e.get("event") == "用户取消高风险指令"]
    assert entries[0]["outcome"] == "cancelled"
    assert entries[0]["pending_action_id"] == str(target.id)


@pytest.mark.asyncio
async def test_cancel_action_logs_already_processed_when_cancel_stmt_matches_nothing(monkeypatch):
    target = _pending_target()

    async def fake_find_target(session, tenant_id, conversation_id):
        return target

    monkeypatch.setattr(command_module, "_find_target_pending_action", fake_find_target)
    session = _FakeSession(execute_results=[_Result(None)])

    with capture_logs() as logs:
        await command_module.cancel_action(_state(), _runtime(session))

    entries = [e for e in logs if e.get("event") == "用户取消高风险指令"]
    assert entries[-1]["outcome"] == "already_processed"
