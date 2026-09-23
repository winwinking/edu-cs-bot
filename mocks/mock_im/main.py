"""mock-im：一个网页聊天界面，扮演 IM 客户端直接连 gateway 的 WebSocket，用于演示和手工测试。

它本身不参与业务逻辑——页面里的 JS 直接连 gateway，这个后端只负责把 HTML 页面发出去。
"""
import os
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI(title="mock-im")

_TEMPLATE_PATH = Path(__file__).parent / "templates" / "index.html"
# 这个 WebSocket 是浏览器直接连的，浏览器在宿主机上，所以要注入宿主机映射端口，不是容器内部端口
_GATEWAY_HOST_PORT = os.getenv("GATEWAY_HOST_PORT", "8000")


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    html = _TEMPLATE_PATH.read_text(encoding="utf-8")
    return html.replace("__GATEWAY_HOST_PORT__", _GATEWAY_HOST_PORT)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
