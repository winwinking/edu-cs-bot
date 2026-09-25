"""覆盖 PHASE3.md 第 5 步重试/熔断的验证清单（LLM 这一侧）：超时最多重试 1 次；重试次数不超过
上限；熔断打开时直接抛异常、根本不会真的调 LLM。

每个用例都换一个全新的 CircuitBreaker，避免上一个用例留下的失败计数/熔断状态漏到下一个
用例（模块级单例 `_breaker` 在整个测试进程里是共享的）。
"""
from types import SimpleNamespace

import httpx
import pytest
from openai import APITimeoutError

from app.common import llm_client as llm_client_module
from app.common.circuit_breaker import CircuitBreaker, CircuitBreakerOpenError


class _FakeCompletions:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        result = self._responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class _FakeClient:
    def __init__(self, responses):
        self.chat = SimpleNamespace(completions=_FakeCompletions(responses))


@pytest.fixture(autouse=True)
def fresh_breaker(monkeypatch):
    monkeypatch.setattr(
        llm_client_module, "_breaker", CircuitBreaker(name="llm", failure_threshold=5, open_seconds=30)
    )
    monkeypatch.setattr(llm_client_module.settings, "llm_retry_backoff_seconds", 0)
    monkeypatch.setattr(llm_client_module.settings, "llm_max_retries", 1)


def _timeout_error() -> APITimeoutError:
    return APITimeoutError(httpx.Request("POST", "http://mock-llm:8000/v1/chat/completions"))


@pytest.mark.asyncio
async def test_chat_completion_retries_once_on_timeout_then_succeeds(monkeypatch):
    fake_result = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])
    fake_client = _FakeClient([_timeout_error(), fake_result])
    monkeypatch.setattr(llm_client_module, "llm_client", fake_client)

    result = await llm_client_module.chat_completion(messages=[{"role": "user", "content": "hi"}])

    assert result is fake_result
    assert fake_client.chat.completions.calls == 2


@pytest.mark.asyncio
async def test_chat_completion_stops_after_max_retries(monkeypatch):
    fake_client = _FakeClient([_timeout_error(), _timeout_error()])
    monkeypatch.setattr(llm_client_module, "llm_client", fake_client)

    with pytest.raises(APITimeoutError):
        await llm_client_module.chat_completion(messages=[{"role": "user", "content": "hi"}])

    assert fake_client.chat.completions.calls == 2  # 1 次 + 最多 1 次重试，不会有第 3 次


@pytest.mark.asyncio
async def test_chat_completion_raises_circuit_open_without_calling_client(monkeypatch):
    breaker = CircuitBreaker(name="llm", failure_threshold=1, open_seconds=30)
    breaker.record_failure()  # 触发熔断
    monkeypatch.setattr(llm_client_module, "_breaker", breaker)

    fake_client = _FakeClient([])
    monkeypatch.setattr(llm_client_module, "llm_client", fake_client)

    with pytest.raises(CircuitBreakerOpenError):
        await llm_client_module.chat_completion(messages=[{"role": "user", "content": "hi"}])

    assert fake_client.chat.completions.calls == 0  # 熔断打开时根本没有真的发请求


@pytest.mark.asyncio
async def test_chat_completion_success_records_success_and_closes_breaker(monkeypatch):
    breaker = CircuitBreaker(name="llm", failure_threshold=1, open_seconds=30)
    breaker.record_failure()
    breaker._opened_at = 0.0
    monkeypatch.setattr("app.common.circuit_breaker.time.monotonic", lambda: 100.0)
    monkeypatch.setattr(llm_client_module, "_breaker", breaker)

    fake_result = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])
    fake_client = _FakeClient([fake_result])
    monkeypatch.setattr(llm_client_module, "llm_client", fake_client)

    result = await llm_client_module.chat_completion(messages=[{"role": "user", "content": "hi"}])
    assert result is fake_result
    assert breaker.state == "closed"
