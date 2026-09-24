"""调用 mock-finance 的客户端（PHASE2.md 2.9 第 2 点）。

超时 1.5 秒；只对超时、5xx、连接错误重试 1 次，间隔 200ms——403/401 不重试，因为重试不会让
"越权"或"服务令牌不对"变成"通过"，白白多打一次请求、多等一次延迟。
"""
import asyncio
from typing import Any, Optional

import httpx

from app.common.config import get_settings

settings = get_settings()

_RETRYABLE_STATUS = {500, 502, 503, 504}
_RETRY_BACKOFF_SECONDS = 0.2

_KIND_TO_PATH = {
    "orders": "/orders",
    "bills": "/bills",
    "invoices": "/invoices",
    "refunds": "/refunds",
    "balance": "/balance",
}


class FinanceForbidden(Exception):
    """mock-finance 自己也拒绝了（403）——两层权限校验里独立的第二层，是正常业务结果，不是 bug。"""


class FinanceUnavailable(Exception):
    """超时、连接失败、服务令牌配错（401，不该发生但也不能让用户看到内部错误）、或重试完还是 5xx。"""


async def fetch_finance_data(
    kind: str, *, tenant_id: str, acting_user_id: str, target_user_id: str, period: Optional[str] = None
) -> dict[str, Any]:
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
    for attempt in range(2):  # 最多 1 次 + 1 次重试
        if attempt > 0:
            await asyncio.sleep(_RETRY_BACKOFF_SECONDS)
        try:
            async with httpx.AsyncClient(timeout=settings.finance_timeout_seconds) as client:
                resp = await client.get(f"{settings.mock_finance_base_url}{path}", params=params, headers=headers)
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            last_error = exc
            continue

        if resp.status_code == 401:
            # 服务令牌配错是我们自己的配置问题，不是用户越权，也不该把内部细节暴露给用户
            raise FinanceUnavailable(f"财务系统鉴权失败：{resp.text}")
        if resp.status_code == 403:
            raise FinanceForbidden()
        if resp.status_code in _RETRYABLE_STATUS:
            last_error = httpx.HTTPStatusError(f"上游 {resp.status_code}", request=resp.request, response=resp)
            continue

        resp.raise_for_status()
        return resp.json()

    raise FinanceUnavailable(str(last_error))
