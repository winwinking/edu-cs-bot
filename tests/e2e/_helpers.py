"""E2E 用例复用 tests/integration 里已经写好的连接/发消息工具（同一套 gateway/DB 连接逻辑，
没必要抄两份），见那边 _helpers.py 的 docstring。E2E 和 integration 的区别不是"连接方式"，
是"测的是不是完整用户可见链路"——E2E 一律从 WebSocket 发消息进去，看最终回复/DB/审计。
"""
import asyncio
from typing import Awaitable, Callable, Optional, TypeVar

from tests.integration._helpers import (
    GATEWAY_URL,
    conversation_id,
    get_token,
    reset_mock,
    send_and_wait,
    set_mock_mode,
)

__all__ = [
    "GATEWAY_URL",
    "conversation_id",
    "get_token",
    "poll_until",
    "reset_mock",
    "send_and_wait",
    "set_mock_mode",
]

_T = TypeVar("_T")


async def poll_until(
    fetch: Callable[[], Awaitable[Optional[_T]]], *, timeout: float = 5, interval: float = 0.2
) -> Optional[_T]:
    """worker/scheduler 把"回复已经发给用户"和"这条回复写进数据库"分成两步（先流式发给用户，
    发完之后才落库，见 app/worker/handler.py），WebSocket 客户端收到 reply_end 的那一刻，
    对应的数据库写入不一定已经提交完成——两者之间隔着一次网络往返，虽然通常只有几毫秒，但不
    应该假设它一定发生在客户端收到消息之前。查"刚发完消息之后立刻要看的数据库状态"时用这个
    轮询，而不是查一次就断言，避免测试结果随机器负载/网络抖动变得不稳定。
    """
    deadline = asyncio.get_event_loop().time() + timeout
    result: Optional[_T] = None
    while asyncio.get_event_loop().time() < deadline:
        result = await fetch()
        if result is not None:
            return result
        await asyncio.sleep(interval)
    return result
