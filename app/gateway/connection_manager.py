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
        tenant_id, user_id = key
        name = channel_name(tenant_id, user_id)
        pubsub = redis_client.pubsub()
        await pubsub.subscribe(name)
        try:
            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue
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
