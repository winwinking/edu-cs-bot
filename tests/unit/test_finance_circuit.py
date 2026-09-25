"""覆盖财务查询这一侧的熔断（PHASE3.md 第 5 步）：熔断打开时直接抛 FinanceCircuitOpen，
根本不发 HTTP 请求；退避间隔带随机抖动，落在 [base, base+jitter) 区间内。
"""
import pytest

from app.common import finance_client as finance_client_module
from app.common.circuit_breaker import CircuitBreaker
from app.common.finance_client import FinanceCircuitOpen, fetch_finance_data


@pytest.mark.asyncio
async def test_fetch_finance_data_raises_circuit_open_without_http_call(monkeypatch):
    breaker = CircuitBreaker(name="finance", failure_threshold=1, open_seconds=30)
    breaker.record_failure()
    monkeypatch.setattr(finance_client_module, "_breaker", breaker)

    called = False

    class _ShouldNotBeCalled:
        def __init__(self, *args, **kwargs):
            nonlocal called
            called = True

    monkeypatch.setattr(finance_client_module.httpx, "AsyncClient", _ShouldNotBeCalled)

    with pytest.raises(FinanceCircuitOpen):
        await fetch_finance_data("balance", tenant_id="t_a", acting_user_id="u_a_1001", target_user_id="u_a_1001")
    assert called is False


def test_retry_backoff_includes_jitter(monkeypatch):
    monkeypatch.setattr(finance_client_module.settings, "finance_retry_backoff_seconds", 0.2)
    monkeypatch.setattr(finance_client_module.settings, "finance_retry_backoff_jitter_seconds", 0.1)

    samples = [finance_client_module._retry_backoff_seconds() for _ in range(50)]
    assert all(0.2 <= s < 0.3 for s in samples)
    # 抖动是随机的，50 个样本里不应该全部相同（不然等于没抖动）
    assert len(set(samples)) > 1
