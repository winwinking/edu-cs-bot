"""覆盖 2.12 审查时要求补的两个并发场景（不连真实 Redis，用手写的假 pubsub/假 redis 客户端模拟）：

(a) 旧的监听任务一旦被新任务换下场，就算它手上已经攒着一条还没处理完的消息，也不会转发出去——
    这是"同一条消息被推两次"这个 bug 真正被堵住的地方，不依赖 cancel() 的真实时机。
(b) disconnect() 不会因为旧任务退订慢（pubsub.unsubscribe/close 耗时）而被拖住，后续的
    connect()/disconnect() 也不会被这把全局锁卡住——这是修第一版方案（持锁 await 旧任务退出）
    时自己引入的死锁，见 AGENT_LOG 步骤 2.12。
"""
import asyncio

import pytest

from app.gateway import connection_manager
from app.gateway.connection_manager import ConnectionManager, channel_name


class _FakeWebSocket:
    def __init__(self):
        self.sent: list[str] = []

    async def send_text(self, data: str) -> None:
        self.sent.append(data)


class _FakePubSub:
    def __init__(self, registry: dict, close_delay: float = 0.0):
        self._registry = registry
        self._close_delay = close_delay
        self._queue: asyncio.Queue = asyncio.Queue()
        self._name: str | None = None

    async def subscribe(self, name: str) -> None:
        self._name = name
        self._registry.setdefault(name, []).append(self)

    async def listen(self):
        while True:
            yield await self._queue.get()

    async def unsubscribe(self, name: str) -> None:
        subs = self._registry.get(name, [])
        if self in subs:
            subs.remove(self)

    async def close(self) -> None:
        if self._close_delay:
            await asyncio.sleep(self._close_delay)

    def push(self, data: str) -> None:
        self._queue.put_nowait({"type": "message", "data": data})


class _FakeRedisClient:
    def __init__(self, close_delay: float = 0.0):
        self.registry: dict[str, list[_FakePubSub]] = {}
        self._close_delay = close_delay

    def pubsub(self) -> _FakePubSub:
        return _FakePubSub(self.registry, close_delay=self._close_delay)

    def publish(self, name: str, data: str) -> None:
        for ps in list(self.registry.get(name, [])):
            ps.push(data)


@pytest.mark.asyncio
async def test_superseded_listener_does_not_forward_a_message_it_was_already_holding(monkeypatch):
    """模拟"旧任务正卡在 pubsub.listen() 内部某次已经收到但还没处理完的消息上"这个窗口期：
    先把一条消息塞进它的假订阅队列，再把 _listener_tasks[key] 登记成别的任务（模拟已经被
    新连接换下场），然后才真正跑这个旧任务——它应该发现自己被换下场，一条都不转发，
    干净退出（该退订的也退订了）。"""
    fake_redis = _FakeRedisClient()
    monkeypatch.setattr(connection_manager, "redis_client", fake_redis)

    manager = ConnectionManager()
    key = ("t_a", "u_a_1001")
    conn = _FakeWebSocket()
    manager._connections[key] = {conn}
    # 模拟"已经被换下场"：当前登记的任务是别的哨兵对象，不是等会儿真正在跑的这个任务
    manager._listener_tasks[key] = object()

    name = channel_name(*key)
    task = asyncio.ensure_future(manager._listen(key))
    await asyncio.sleep(0)  # 让它跑到 subscribe() 完成、开始等消息
    fake_redis.publish(name, "hello")
    await asyncio.wait_for(task, timeout=1)

    assert conn.sent == []  # 一次都没转发给这个连接
    assert fake_redis.registry.get(name, []) == []  # finally 里的退订确实跑到了


@pytest.mark.asyncio
async def test_disconnect_does_not_block_even_if_old_listener_teardown_is_slow(monkeypatch):
    """旧任务的 pubsub.unsubscribe()/close() 故意设成很慢（模拟 Redis 网络抖动、连接关闭慢），
    disconnect() 和随后的 connect() 都必须很快返回——不能因为等旧任务退订完成而卡住。
    第一版修复（disconnect() 里 await 旧任务真正退出）在这个场景下会一直卡到 close_delay 结束，
    这里用 asyncio.wait_for 给一个远小于 close_delay 的超时，卡住就会直接超时失败。"""
    fake_redis = _FakeRedisClient(close_delay=5.0)
    monkeypatch.setattr(connection_manager, "redis_client", fake_redis)

    manager = ConnectionManager()
    conn1 = _FakeWebSocket()
    await manager.connect("t_a", "u_a_1001", conn1)
    await asyncio.sleep(0)  # 让监听任务跑到订阅完成、阻塞等消息，模拟真实连接已经建立好

    await asyncio.wait_for(manager.disconnect("t_a", "u_a_1001", conn1), timeout=0.5)

    conn2 = _FakeWebSocket()
    await asyncio.wait_for(manager.connect("t_a", "u_a_1001", conn2), timeout=0.5)

    assert conn2 in manager._connections[("t_a", "u_a_1001")]
