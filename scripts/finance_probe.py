"""绕过 worker，直接以某人身份请求 mock-finance（阶段二 2.9）。

用来证明就算 worker 那层权限校验出了问题，mock-finance 自己也会独立拒绝越权——两层校验，
少了任何一层另一层还能挡住。

用法：
  python scripts/finance_probe.py --tenant t_a --acting u_a_1001 --target u_a_1004 --kind invoices
"""
import argparse
import asyncio

import httpx

from app.common.config import get_settings

settings = get_settings()

_KIND_TO_PATH = {
    "orders": "/orders",
    "bills": "/bills",
    "invoices": "/invoices",
    "refunds": "/refunds",
    "balance": "/balance",
}


async def run(tenant: str, acting: str, target: str, kind: str, period: str | None) -> None:
    params = {"user_id": target}
    if period:
        params["period"] = period
    headers = {
        "X-Service-Token": settings.finance_service_token,
        "X-Tenant-Id": tenant,
        "X-Acting-User-Id": acting,
    }
    async with httpx.AsyncClient(timeout=5) as client:
        resp = await client.get(f"{settings.mock_finance_base_url}{_KIND_TO_PATH[kind]}", params=params, headers=headers)

    print(f"status={resp.status_code}")
    print(resp.text)


def main() -> None:
    parser = argparse.ArgumentParser(description="直接探测 mock-finance 的权限校验，绕开 worker")
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--acting", required=True, help="发起查询的人")
    parser.add_argument("--target", required=True, help="被查询的人")
    parser.add_argument("--kind", required=True, choices=sorted(_KIND_TO_PATH))
    parser.add_argument("--period", default=None)
    args = parser.parse_args()
    asyncio.run(run(args.tenant, args.acting, args.target, args.kind, args.period))


if __name__ == "__main__":
    main()
