"""覆盖 PHASE3.md 第 5 步熔断器的验证清单：连续失败到阈值熔断、到时间放试探、
试探成功恢复、试探失败继续熔断；403/404 这类业务结果不算失败（由调用方决定要不要
调 record_failure，这里只测 CircuitBreaker 本身的状态机）。
"""
from app.common.circuit_breaker import CircuitBreaker


def test_stays_closed_under_threshold():
    cb = CircuitBreaker(name="t", failure_threshold=5, open_seconds=30)
    for _ in range(4):
        cb.record_failure()
    assert cb.state == "closed"
    assert cb.allow_request() is True


def test_opens_at_threshold():
    cb = CircuitBreaker(name="t", failure_threshold=5, open_seconds=30)
    for _ in range(5):
        cb.record_failure()
    assert cb.state == "open"
    assert cb.allow_request() is False


def test_success_resets_failure_count():
    cb = CircuitBreaker(name="t", failure_threshold=5, open_seconds=30)
    for _ in range(4):
        cb.record_failure()
    cb.record_success()
    cb.record_failure()
    assert cb.state == "closed"  # 成功过一次，之前的失败计数清零，不会因为再失败 1 次就凑够 5


def test_half_open_after_timeout_allows_one_probe(monkeypatch):
    cb = CircuitBreaker(name="t", failure_threshold=1, open_seconds=30)
    fake_now = [1000.0]
    monkeypatch.setattr("app.common.circuit_breaker.time.monotonic", lambda: fake_now[0])

    cb.record_failure()  # 熔断打开
    assert cb.allow_request() is False

    fake_now[0] += 30  # 时间到了，应该进入半开
    assert cb.state == "half_open"
    assert cb.allow_request() is True  # 放一个试探请求
    assert cb.allow_request() is False  # 试探结果还没出来，其它并发请求不再放行


def test_probe_success_closes_breaker(monkeypatch):
    cb = CircuitBreaker(name="t", failure_threshold=1, open_seconds=30)
    fake_now = [1000.0]
    monkeypatch.setattr("app.common.circuit_breaker.time.monotonic", lambda: fake_now[0])

    cb.record_failure()
    fake_now[0] += 30
    assert cb.allow_request() is True
    cb.record_success()
    assert cb.state == "closed"
    assert cb.allow_request() is True


def test_probe_failure_reopens_and_restarts_timer(monkeypatch):
    cb = CircuitBreaker(name="t", failure_threshold=1, open_seconds=30)
    fake_now = [1000.0]
    monkeypatch.setattr("app.common.circuit_breaker.time.monotonic", lambda: fake_now[0])

    cb.record_failure()
    fake_now[0] += 30
    assert cb.allow_request() is True
    cb.record_failure()  # 试探失败，继续熔断
    assert cb.state == "open"

    fake_now[0] += 10  # 还没到新一轮的 30 秒
    assert cb.allow_request() is False

    fake_now[0] += 20  # 凑满新一轮的 30 秒
    assert cb.allow_request() is True
