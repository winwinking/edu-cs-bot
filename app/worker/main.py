"""worker：消费队列，处理业务（意图路由、检索、工具调用留到后续阶段）。
同进程内起一个轻量 HTTP 服务（端口 8001）做健康检查和指标，不是另开一个容器。
"""
import asyncio

import uvicorn
from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.common.logging import configure_logging, get_logger
from app.worker.consumer import run_consumer

configure_logging()
logger = get_logger(__name__)

http_app = FastAPI(title="worker")


@http_app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@http_app.get("/metrics")
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


async def main() -> None:
    connection = await run_consumer()
    # log_config=None：uvicorn 默认会覆盖 root logger 配置，干扰我们自己的 structlog 设置
    config = uvicorn.Config(http_app, host="0.0.0.0", port=8001, log_config=None)
    server = uvicorn.Server(config)
    try:
        await server.serve()
    finally:
        await connection.close()
        logger.info("worker 关闭，MQ 连接已断开")


if __name__ == "__main__":
    asyncio.run(main())
