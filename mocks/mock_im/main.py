"""mock-im：阶段三第 7 步升级成演示控制台——一个网页聊天界面，扮演 IM 客户端直接连 gateway
的 WebSocket，支持身份切换、流式聊天、提醒推送、10 个 E2E 场景快捷发送、reply_end meta 透视。

这个后端只做两件跟业务无关的辅助事：发 HTML 页面、按 tenant_id+user_id 现场签发开发用 token
（`/api/token`，复用 app.common.auth.create_access_token，跟 scripts/chat.py 的签发方式一样，
密钥来自环境变量，不写死在页面或代码里）——聊天本身的收发全部是页面里的 JS 直接连 gateway，
这个后端不转发、不解析业务消息。
"""
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import select

import uvicorn

from app.common.auth import create_access_token
from app.common.db import AsyncSessionLocal
from app.common.models import User

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


@app.get("/api/token")
async def issue_token(tenant_id: str, user_id: str) -> dict:
    """身份切换用：role 只有一个来源——数据库（CLAUDE.md 硬性规则），不能让前端自己传/猜，
    也不能让前端自己拼 JWT（密钥不能出现在页面或浏览器可见的代码里）。"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.tenant_id == tenant_id, User.id == user_id))
        row = result.scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail=f"用户不存在：tenant={tenant_id} user={user_id}，先跑 make seed")
        token = create_access_token(user_id=row.id, tenant_id=row.tenant_id, role=row.role.value)
        return {"token": token, "name": row.name, "role": row.role.value}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
