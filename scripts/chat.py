"""命令行多轮对话工具：生成 token、连 gateway、发一条消息、打印 ack/流式回复/meta（阶段二 2.4）。

用法：
  python scripts/chat.py --tenant t_a --user u_a_1001 --conv c1 "你好"
  # 同一个 --conv 再发一次，延续同一个会话：
  python scripts/chat.py --tenant t_a --user u_a_1001 --conv c1 "继续问"
  # 用同一个 message-id 重发，测试去重：
  python scripts/chat.py --tenant t_a --user u_a_1001 --conv c1 --message-id <上次的id> "..."
"""
import argparse
import asyncio
import json
import uuid

import websockets
from sqlalchemy import select

from app.common.auth import create_access_token
from app.common.db import AsyncSessionLocal
from app.common.models import User

REPLY_TIMEOUT_SECONDS = 30

# 阶段一的 conversation_id 必须是合法 UUID（worker 用 uuid.UUID() 解析），但 PHASE2.md 里
# --conv 给的是人类可读的短标签（如 c1、f2、p1）。这里用 uuid5 把 (tenant, user, 标签) 三元组
# 确定性地映射成同一个 UUID——同一个标签每次算出来的 UUID 都一样，天然支持"同一个 --conv
# 能连续多轮对话"；带上 tenant/user 是因为一个会话本来就只属于一个租户下的一个用户。
_CONVERSATION_NAMESPACE = uuid.UUID("6f8f2c2e-1a4b-4e9a-9f1a-2c9a7e6d5b4a")


def _conversation_id(tenant: str, user: str, conv_label: str) -> str:
    return str(uuid.uuid5(_CONVERSATION_NAMESPACE, f"{tenant}:{user}:{conv_label}"))


async def _get_token(tenant: str, user: str) -> str:
    # 和 gen_token.py 相同的逻辑：role 从数据库查真实值，不让调用方手动传、传错了和数据库不一致
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.tenant_id == tenant, User.id == user))
        row = result.scalar_one_or_none()
        if row is None:
            raise SystemExit(f"用户不存在：tenant={tenant} user={user}，先跑 make seed")
        return create_access_token(user_id=row.id, tenant_id=row.tenant_id, role=row.role.value)


async def run(tenant: str, user: str, conv_label: str, content: str, message_id: str, gateway_url: str) -> None:
    token = await _get_token(tenant, user)
    conversation_id = _conversation_id(tenant, user, conv_label)
    url = f"{gateway_url}?token={token}"

    async with websockets.connect(url) as ws:
        await ws.send(
            json.dumps(
                {
                    "type": "message",
                    "message_id": message_id,
                    "conversation_id": conversation_id,
                    "content": content,
                }
            )
        )

        ack = json.loads(await ws.recv())
        print(f"[ack] status={ack['status']} trace_id={ack.get('trace_id')}")

        if ack["status"] == "duplicate":
            print("这条消息之前发过，不会触发新的回复")
            return

        reply_end_msg: dict | None = None
        try:
            async with asyncio.timeout(REPLY_TIMEOUT_SECONDS):
                while True:
                    msg = json.loads(await ws.recv())
                    if msg["type"] == "reply_chunk":
                        print(msg["delta"], end="", flush=True)
                    elif msg["type"] == "reply_end":
                        reply_end_msg = msg
                        break
                    elif msg["type"] == "error":
                        print(f"\n[error] code={msg['code']} detail={msg['detail']}")
                        return
        except TimeoutError:
            raise SystemExit(f"\n超过 {REPLY_TIMEOUT_SECONDS} 秒没收到 reply_end，超时退出")

        print()
        # 本阶段还没给 reply_end 加 meta 字段（要到 2.7 LangGraph 编排落地才会有），
        # 这里按 PHASE2.md 2.4 的验证预期，取不到就打印空对象
        meta = reply_end_msg.get("meta", {}) if reply_end_msg else {}
        print(f"[meta] {json.dumps(meta, ensure_ascii=False, indent=2)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="命令行多轮对话工具")
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--conv", required=True, help="会话标签，同一个标签连续对话会延续同一个会话")
    parser.add_argument("--message-id", default=None, help="不传就自动生成；传上次用过的可以测试重复")
    parser.add_argument("--gateway-url", default="ws://gateway:8000/ws", help="默认走 docker 网络内部地址")
    parser.add_argument("content", help="消息内容")
    args = parser.parse_args()

    message_id = args.message_id or str(uuid.uuid4())
    asyncio.run(run(args.tenant, args.user, args.conv, args.content, message_id, args.gateway_url))


if __name__ == "__main__":
    main()
