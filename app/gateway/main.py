"""gateway：WebSocket 接入层。鉴权、消息校验、去重、投递队列、ACK、回推回复。不调用 LLM。"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response, WebSocket, WebSocketDisconnect
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.common.auth import TokenError, decode_access_token
from app.common.db import check_db_connection
from app.common.logging import bind_trace_context, clear_trace_context, configure_logging, get_logger
from app.common.mq import declare_topology, get_confirm_channel, get_connection
from app.common.redis import check_redis_connection, note_redis_result

from app.gateway.connection_manager import manager
from app.gateway.message_handler import handle_inbound_message
from app.gateway.metrics import ws_connections

configure_logging()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    connection = await get_connection()
    channel = await get_confirm_channel(connection)
    inbound_exchange, _, _ = await declare_topology(channel)
    app.state.mq_connection = connection
    app.state.mq_channel = channel
    app.state.mq_exchange = inbound_exchange
    logger.info("gateway 启动，MQ 拓扑已就绪")
    yield
    await connection.close()
    logger.info("gateway 关闭，MQ 连接已断开")


app = FastAPI(title="gateway", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    # Redis 不可用时仍然返回 200（设计决定 9：限流/去重/推送都能在没有 Redis 的情况下降级运行，
    # gateway 本身没有挂，不该被存活探针重启），只在返回内容里标出降级状态给人看
    redis_ok = await check_redis_connection()
    note_redis_result(redis_ok)
    return {"status": "ok" if redis_ok else "degraded", "redis": "up" if redis_ok else "down"}


@app.get("/ready")
async def ready(response: Response) -> dict:
    db_ok = await check_db_connection()
    redis_ok = await check_redis_connection()
    mq_ok = not app.state.mq_connection.is_closed
    ok = db_ok and redis_ok and mq_ok
    response.status_code = 200 if ok else 503
    return {"postgres": db_ok, "redis": redis_ok, "rabbitmq": mq_ok}


@app.get("/metrics")
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    # WS 的 close code 是协议帧的一部分，只有 accept() 完成握手之后才能带自定义 code 发出去；
    # accept() 之前调 close() 只会被降级成普通的 HTTP 403，客户端看不到 4401，所以必须先 accept 再关
    await websocket.accept()

    token = websocket.query_params.get("token")
    if not token:
        logger.warning("WebSocket 缺少 token")
        await websocket.close(code=4401)
        return

    try:
        payload = decode_access_token(token)
    except TokenError:
        # 日志不记录 token 本身，只记录校验失败这件事
        logger.warning("WebSocket 鉴权失败")
        await websocket.close(code=4401)
        return

    tenant_id = payload.tenant_id
    user_id = payload.sub

    bind_trace_context(tenant_id=tenant_id)
    logger.info("WebSocket 连接建立", user_id=user_id)

    ws_connections.inc()
    await manager.connect(tenant_id, user_id, websocket)
    try:
        while True:
            raw = await websocket.receive_text()
            await handle_inbound_message(
                websocket,
                tenant_id=tenant_id,
                user_id=user_id,
                raw_text=raw,
                exchange=websocket.app.state.mq_exchange,
            )
    except WebSocketDisconnect:
        logger.info("WebSocket 连接断开", user_id=user_id)
    finally:
        await manager.disconnect(tenant_id, user_id, websocket)
        ws_connections.dec()
        clear_trace_context()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
