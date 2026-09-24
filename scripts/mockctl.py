"""统一控制 mock 服务的运行时配置（阶段二 2.4）。

用法：
  python scripts/mockctl.py llm mode=invalid_json
  python scripts/mockctl.py finance mode=timeout
  python scripts/mockctl.py platform agents_online=false
  python scripts/mockctl.py llm show
  python scripts/mockctl.py all reset

finance/platform 的 /admin/config 要到阶段二 2.9/2.10 才会实现，现在调用会报错，这是预期的——
这个工具是通用的，接口一实现就能直接用，不用等它们做完再回头改 mockctl.py。
"""
import argparse
import asyncio
import json

import httpx

SERVICES = ["llm", "finance", "platform"]
TIMEOUT_SECONDS = 5  # 手工控制工具，给足时间但不能无限等


def _base_url(service: str) -> str:
    # 所有 mock 服务在 docker 网络内部都叫 mock-{name}，监听 8000，这是 docker-compose.yml 里
    # 固定的命名规则，不需要为此专门加一套 Settings
    return f"http://mock-{service}:8000"


def _parse_value(raw: str) -> bool | int | float | str:
    if raw.lower() in ("true", "false"):
        return raw.lower() == "true"
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


async def _show(client: httpx.AsyncClient, service: str) -> None:
    resp = await client.get(f"{_base_url(service)}/admin/config")
    resp.raise_for_status()
    print(f"[{service}] {json.dumps(resp.json(), ensure_ascii=False)}")


async def _reset(client: httpx.AsyncClient, service: str) -> None:
    resp = await client.post(f"{_base_url(service)}/admin/reset")
    resp.raise_for_status()
    print(f"[{service}] 已重置：{json.dumps(resp.json(), ensure_ascii=False)}")


async def _update(client: httpx.AsyncClient, service: str, updates: dict) -> None:
    resp = await client.post(f"{_base_url(service)}/admin/config", json=updates)
    resp.raise_for_status()
    print(f"[{service}] 已更新：{json.dumps(resp.json(), ensure_ascii=False)}")


async def main(service: str, args: list[str]) -> None:
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        if service == "all":
            if args != ["reset"]:
                raise SystemExit("all 只支持 reset：python scripts/mockctl.py all reset")
            for s in SERVICES:
                try:
                    await _reset(client, s)
                except httpx.HTTPError as exc:
                    print(f"[{s}] 跳过（{exc.__class__.__name__}：这个 mock 可能还没实现 /admin/reset）")
            return

        if service not in SERVICES:
            raise SystemExit(f"未知的服务：{service}，可选：{SERVICES + ['all']}")

        if args == ["show"]:
            await _show(client, service)
        elif args == ["reset"]:
            await _reset(client, service)
        else:
            updates: dict = {}
            for arg in args:
                key, sep, value = arg.partition("=")
                if not sep:
                    raise SystemExit(f"参数格式不对：{arg!r}，应该是 key=value")
                updates[key] = _parse_value(value)
            await _update(client, service, updates)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="统一控制 mock 服务配置")
    parser.add_argument("service", help="llm / finance / platform / all")
    parser.add_argument("args", nargs="+", help="show / reset / key=value ...")
    parsed = parser.parse_args()
    asyncio.run(main(parsed.service, parsed.args))
