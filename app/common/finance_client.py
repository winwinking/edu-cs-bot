"""调用 mock-finance 的客户端（PHASE2.md 2.9 第 2 点；阶段三第 5 步加熔断和带抖动的退避）。

超时 1.5 秒；只对超时、5xx、连接错误重试（次数见配置），退避间隔加随机抖动——403/401 不重试，
因为重试不会让"越权"或"服务令牌不对"变成"通过"，白白多打一次请求、多等一次延迟。抖动是为了
避免同一秒内一批请求同时超时后又同时在同一时刻重试，撞在一起给刚恢复的上游又来一波压力。
"""
import asyncio
import random
from typing import Any, Optional

import httpx

from app.common.circuit_breaker import CircuitBreaker, CircuitBreakerOpenError
from app.common.config import get_settings

settings = get_settings()

_RETRYABLE_STATUS = {500, 502, 503, 504}

_KIND_TO_PATH = {
    "orders": "/orders",
    "bills": "/bills",
    "invoices": "/invoices",
    "refunds": "/refunds",
    "balance": "/balance",
}

_breaker = CircuitBreaker(
    name="finance", failure_threshold=settings.cb_failure_threshold, open_seconds=settings.cb_open_seconds
)


def finance_circuit_state() -> str:
    """给指标/排障用，不参与业务判断"""
    return _breaker.state


class FinanceForbidden(Exception):
    """mock-finance 自己也拒绝了（403）——两层权限校验里独立的第二层，是正常业务结果，不是 bug。"""


class FinanceUnavailable(Exception):
    """超时、连接失败、服务令牌配错（401，不该发生但也不能让用户看到内部错误）、或重试完还是 5xx。"""


class FinanceCircuitOpen(FinanceUnavailable):
    """熔断打开，这次调用直接被拦下、根本没有真的发请求——业务节点用这个类型区分"是熔断降级"
    还是"这一次调用本身超时/出错"，好在 meta 里标出熔断标记。"""


def _retry_backoff_seconds() -> float:
    return settings.finance_retry_backoff_seconds + random.uniform(0, settings.finance_retry_backoff_jitter_seconds)


async def fetch_finance_data(
    kind: str, *, tenant_id: str, acting_user_id: str, target_user_id: str, period: Optional[str] = None
) -> dict[str, Any]:
    if not _breaker.allow_request():
        raise FinanceCircuitOpen("finance")

    path = _KIND_TO_PATH[kind]
    params: dict[str, str] = {"user_id": target_user_id}
    if period is not None:
        params["period"] = period
    headers = {
        "X-Service-Token": settings.finance_service_token,
        "X-Tenant-Id": tenant_id,
        "X-Acting-User-Id": acting_user_id,
    }

    last_error: Optional[BaseException] = None
    for attempt in range(settings.finance_max_retries + 1):
        if attempt > 0:
            await asyncio.sleep(_retry_backoff_seconds())
        try:
            async with httpx.AsyncClient(timeout=settings.finance_timeout_seconds) as client:
                resp = await client.get(f"{settings.mock_finance_base_url}{path}", params=params, headers=headers)
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            last_error = exc
            continue

        if resp.status_code == 401:
            # 服务令牌配错是我们自己的配置问题，不是用户越权，也不该把内部细节暴露给用户；
            # 不算熔断意义上的"失败"（不是上游真的挂了，是我们自己配错），不计入熔断计数
            raise FinanceUnavailable(f"财务系统鉴权失败：{resp.text}")
        if resp.status_code == 403:
            # 业务拒绝，不是故障，同样不计入熔断计数
            raise FinanceForbidden()
        if resp.status_code in _RETRYABLE_STATUS:
            last_error = httpx.HTTPStatusError(f"上游 {resp.status_code}", request=resp.request, response=resp)
            continue

        resp.raise_for_status()
        _breaker.record_success()
        return resp.json()

    _breaker.record_failure()
    raise FinanceUnavailable(str(last_error))
