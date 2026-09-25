"""scheduler：提醒调度。跟 gateway/worker 共用同一个镜像，靠 docker-compose 的 command 区分。
同进程内起一个轻量 HTTP 服务（端口 8002）做健康检查，跟 gateway(8000)/worker(8001) 一个套路。
"""
import asyncio

import uvicorn
from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.common.logging import configure_logging, get_logger
from app.scheduler.loop import run_scheduler_loop

configure_logging()
logger = get_logger(__name__)

http_app = FastAPI(title="scheduler")


@http_app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@http_app.get("/metrics")
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


async def main() -> None:
    loop_task = asyncio.create_task(run_scheduler_loop())
    # log_config=None：uvicorn 默认会覆盖 root logger 配置，干扰我们自己的 structlog 设置
    config = uvicorn.Config(http_app, host="0.0.0.0", port=8002, log_config=None)
    server = uvicorn.Server(config)
    try:
        await server.serve()
    finally:
        loop_task.cancel()
        logger.info("scheduler 关闭")


if __name__ == "__main__":
    asyncio.run(main())
