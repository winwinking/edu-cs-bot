"""管理本实例上的 WebSocket 连接，并把 Redis 频道的消息转发给对应用户的所有连接。

同一个用户可能开多个端（手机+网页），所以按 (tenant_id, user_id) 分组维护连接集合，
一个用户的第一个连接建立时才订阅 Redis 频道，最后一个连接断开时才退订——
避免每个连接都单独订阅一次频道，造成重复推送和资源浪费。
"""
import asyncio
from typing import Dict, Set, Tuple

from fastapi import WebSocket

from app.common.logging import get_logger
from app.common.redis import redis_client

logger = get_logger(__name__)

ConnectionKey = Tuple[str, str]  # (tenant_id, user_id)


def channel_name(tenant_id: str, user_id: str) -> str:
    return f"im:out:{tenant_id}:{user_id}"


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: Dict[ConnectionKey, Set[WebSocket]] = {}
        self._listener_tasks: Dict[ConnectionKey, asyncio.Task] = {}
        # 同一个 key 的 connect/disconnect 可能并发触发（多个端几乎同时连上/断开），
        # 用锁保护"连接集合是否从空变非空/从非空变空"这个判断，不然可能重复订阅或漏订阅
        self._lock = asyncio.Lock()

    async def connect(self, tenant_id: str, user_id: str, websocket: WebSocket) -> None:
        key = (tenant_id, user_id)
        async with self._lock:
            conns = self._connections.setdefault(key, set())
            is_first_connection = len(conns) == 0
            conns.add(websocket)
            if is_first_connection:
                self._listener_tasks[key] = asyncio.create_task(self._listen(key))

    async def disconnect(self, tenant_id: str, user_id: str, websocket: WebSocket) -> None:
        key = (tenant_id, user_id)
        async with self._lock:
            conns = self._connections.get(key)
            if conns is None:
                return
            conns.discard(websocket)
            if not conns:
                del self._connections[key]
                task = self._listener_tasks.pop(key, None)
                if task:
                    task.cancel()

    async def _listen(self, key: ConnectionKey) -> None:
        """cancel() 只是发起取消，不保证任务立刻停止——_listen() 可能正卡在 pubsub.listen()
        内部某次已经收到但还没处理完的消息上。如果这时正好有新连接冒出来（同一个用户快速断线
        重连很常见），新连接会看到"当前 0 个连接"从而新建第二个监听任务；旧任务这时还没来得及
        退订，会和新任务同时订阅同一个频道，导致同一条消息被推给客户端两次（这是 2.12 写冒烟
        测试连续快速重连同一个用户时复现出来的竞态）。

        这里不去同步等旧任务退订完成（那样得在拿着 self._lock 的时候 await，一旦 unsubscribe
        卡住会把这把全局锁焊死，后续所有连接都会跟着永久挂起——已经试过这个方案，会导致死锁）。
        改成每次转发前自己检查一下"当前登记在 _listener_tasks[key] 下的是不是还是我自己"：
        一旦被换下场（新任务已经顶替了这个 key），旧任务立刻停止转发并退出，不需要等 cancel()
        真正生效，也不会有两个任务同时转发的窗口。
        """
        tenant_id, user_id = key
        name = channel_name(tenant_id, user_id)
        this_task = asyncio.current_task()
        pubsub = redis_client.pubsub()
        await pubsub.subscribe(name)
        try:
            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue
                if self._listener_tasks.get(key) is not this_task:
                    break
                data = message["data"]
                # 发送给这个用户当前所有本地连接；某个连接发送失败不影响其他连接，
                # 失败的连接会在自己的 receive 循环里触发 WebSocketDisconnect 并被清理
                for ws in list(self._connections.get(key, ())):
                    try:
                        await ws.send_text(data)
                    except Exception:
                        logger.warning("推送给某个 WebSocket 连接失败", tenant_id=tenant_id, user_id=user_id)
        except asyncio.CancelledError:
            pass
        finally:
            await pubsub.unsubscribe(name)
            await pubsub.close()

    @property
    def connection_count(self) -> int:
        return sum(len(conns) for conns in self._connections.values())


manager = ConnectionManager()
