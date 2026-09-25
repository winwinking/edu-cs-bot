"""调用 mock-platform 的客户端（PHASE2.md 2.10 第 2 点）。

超时 3 秒；只对超时、5xx、连接错误重试，最多 2 次（次数和退避基数见配置：第 N 次重试等
base*N 秒，默认 0.5 秒和 1 秒，跟阶段二时一样，只是改成从配置读，不再写死在代码里）；
4xx 不重试——4xx 是参数问题，重试不会变成 2xx。因为每次执行指令都带幂等键，重试也不会让
mock-platform 把同一个指令执行两次。
"""
import asyncio
from typing import Any, Optional

import httpx

from app.common.config import get_settings
from app.common.logging import get_logger

settings = get_settings()
logger = get_logger(__name__)

_RETRYABLE_STATUS = {500, 502, 503, 504}


class PlatformUnavailable(Exception):
    """超时、连接失败、或重试用完仍失败——不把内部错误细节暴露给用户，统一走固定话术。"""


async def _request(
    method: str, path: str, *, json_body: Optional[dict] = None, params: Optional[dict] = None
) -> dict[str, Any]:
    last_error: Optional[BaseException] = None
    max_attempts = settings.platform_max_retries + 1
    for attempt in range(max_attempts):
        if attempt > 0:
            await asyncio.sleep(settings.platform_retry_backoff_base_seconds * attempt)
        logger.info(
            "调用 mock-platform",
            method=method,
            path=path,
            attempt=attempt + 1,
            idempotency_key=(json_body or {}).get("idempotency_key"),
        )
        try:
            async with httpx.AsyncClient(timeout=settings.platform_timeout_seconds) as client:
                resp = await client.request(
                    method, f"{settings.mock_platform_base_url}{path}", json=json_body, params=params
                )
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            last_error = exc
            continue

        if resp.status_code in _RETRYABLE_STATUS:
            last_error = httpx.HTTPStatusError(f"上游 {resp.status_code}", request=resp.request, response=resp)
            continue
        if resp.status_code >= 400:
            # 4xx 不重试，直接失败——参数或请求本身有问题，重试也不会变成 2xx
            raise PlatformUnavailable(f"mock-platform 返回 {resp.status_code}：{resp.text}")

        return resp.json()

    raise PlatformUnavailable(str(last_error))


async def submit_command(*, tenant_id: str, user_id: str, action: str, params: dict, idempotency_key: str) -> dict:
    body = {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "action": action,
        "params": params,
        "idempotency_key": idempotency_key,
    }
    return await _request("POST", "/commands", json_body=body)


async def get_subscriptions(tenant_id: str, user_id: str) -> list[dict]:
    data = await _request("GET", f"/users/{user_id}/subscriptions", params={"tenant_id": tenant_id})
    return data["subscriptions"]


async def get_agents_status() -> dict:
    return await _request("GET", "/agents/status")
