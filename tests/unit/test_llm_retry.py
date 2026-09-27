"""覆盖 PHASE3.md 第 5 步重试/熔断的验证清单（LLM 这一侧），以及阶段四故障注入 9 审查修复的
超时行为：超时不重试、直接失败（这样才能真的在配置的超时秒数内失败降级，不会因为"超时再重试
一次"把总等待时间翻倍——故障注入 9 发现的问题就是原来超时会重试 1 次，最坏情况一步要等
约 30 秒）；500 及以上状态码、连接失败这类"立刻能知道失败"的错误仍然重试 1 次；不管是不是
重试，超时都要按"一次调用失败"计入熔断。

每个用例都换一个全新的 CircuitBreaker，避免上一个用例留下的失败计数/熔断状态漏到下一个
用例（模块级单例 `_breaker` 在整个测试进程里是共享的）。

本文件本轮（PHASE4.md 故障注入期间）没有实际执行，等 Jo 通知故障注入结束后随 make test 一起跑。
"""
from types import SimpleNamespace

import httpx
import pytest
from openai import APIStatusError, APITimeoutError

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


def _status_error(status_code: int) -> APIStatusError:
    request = httpx.Request("POST", "http://mock-llm:8000/v1/chat/completions")
    response = httpx.Response(status_code=status_code, request=request)
    return APIStatusError("mock 上游错误", response=response, body=None)


@pytest.mark.asyncio
async def test_chat_completion_does_not_retry_on_timeout(monkeypatch):
    # 只喂一个超时异常：如果代码还在重试，_FakeCompletions 第二次 create() 会因为
    # responses 列表空了直接 IndexError，用这个反过来证明"确实只发了 1 次请求"
    fake_client = _FakeClient([_timeout_error()])
    monkeypatch.setattr(llm_client_module, "llm_client", fake_client)

    with pytest.raises(APITimeoutError):
        await llm_client_module.chat_completion(messages=[{"role": "user", "content": "hi"}])

    assert fake_client.chat.completions.calls == 1


@pytest.mark.asyncio
async def test_timeout_still_counts_as_one_circuit_breaker_failure(monkeypatch):
    # failure_threshold=1：一次超时就应该直接把熔断打开，证明"超时不重试"不等于"超时不计入熔断"
    breaker = CircuitBreaker(name="llm", failure_threshold=1, open_seconds=30)
    monkeypatch.setattr(llm_client_module, "_breaker", breaker)

    fake_client = _FakeClient([_timeout_error()])
    monkeypatch.setattr(llm_client_module, "llm_client", fake_client)

    with pytest.raises(APITimeoutError):
        await llm_client_module.chat_completion(messages=[{"role": "user", "content": "hi"}])

    assert breaker.state == "open"


@pytest.mark.asyncio
async def test_chat_completion_retries_once_on_500_then_succeeds(monkeypatch):
    fake_result = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])
    fake_client = _FakeClient([_status_error(500), fake_result])
    monkeypatch.setattr(llm_client_module, "llm_client", fake_client)

    result = await llm_client_module.chat_completion(messages=[{"role": "user", "content": "hi"}])

    assert result is fake_result
    assert fake_client.chat.completions.calls == 2


@pytest.mark.asyncio
async def test_chat_completion_stops_after_max_retries_on_500(monkeypatch):
    fake_client = _FakeClient([_status_error(500), _status_error(500)])
    monkeypatch.setattr(llm_client_module, "llm_client", fake_client)

    with pytest.raises(APIStatusError):
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


@pytest.mark.asyncio
async def test_stream_chat_completion_does_not_retry_on_timeout(monkeypatch):
    # 流式请求建立阶段（还没开始迭代到任何 chunk）命中超时，同样不应该重试——跟非流式是
    # 同一个 _is_retryable()，这里只是确认改动没有漏掉流式这一条调用路径
    fake_client = _FakeClient([_timeout_error()])
    monkeypatch.setattr(llm_client_module, "llm_client", fake_client)

    with pytest.raises(APITimeoutError):
        async for _ in llm_client_module.stream_chat_completion(messages=[{"role": "user", "content": "hi"}]):
            pass

    assert fake_client.chat.completions.calls == 1
