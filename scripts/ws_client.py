"""命令行 WebSocket 客户端：连 gateway 发一条消息，打印 ACK / 首 token / 完整回复三个耗时。

用法：
  python scripts/ws_client.py --token <jwt> --conversation-id <uuid> --content "消息内容"
  # 用同一个 message_id 重发（演示去重）：
  python scripts/ws_client.py --token <jwt> --conversation-id <uuid> --content "..." --message-id <上次的 message_id>
"""
import argparse
import asyncio
import json
import time
import uuid

import websockets


async def run(token: str, conversation_id: str, content: str, message_id: str, gateway_url: str) -> None:
    url = f"{gateway_url}?token={token}"
    t0 = time.monotonic()
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
        print(f"[发送] conversation_id={conversation_id}")
        # 这一行是给 scripts/demo.sh 解析用的，人看上面那行就够了
        print(f"MESSAGE_ID={message_id}")

        ack = json.loads(await ws.recv())
        ack_latency_ms = (time.monotonic() - t0) * 1000
        print(f"[ACK] status={ack['status']} trace_id={ack['trace_id']} 耗时={ack_latency_ms:.1f}ms")

        if ack["status"] == "duplicate":
            print("这条消息之前发过，gateway 判重后不会再触发一次新的回复生成")
            return

        chunks = []
        first_token_latency_ms = None
        while True:
            msg = json.loads(await ws.recv())
            if msg["type"] == "reply_chunk":
                if first_token_latency_ms is None:
                    first_token_latency_ms = (time.monotonic() - t0) * 1000
                    print(f"[首 token] 耗时={first_token_latency_ms:.1f}ms")
                chunks.append(msg["delta"])
            elif msg["type"] == "reply_end":
                break
            elif msg["type"] == "error":
                print(f"[ERROR] code={msg['code']} detail={msg['detail']}")
                return

        total_latency_ms = (time.monotonic() - t0) * 1000
        print(f"[完整回复] {''.join(chunks)}")
        print(f"[完整回复耗时] {total_latency_ms:.1f}ms")


def main() -> None:
    parser = argparse.ArgumentParser(description="命令行 WebSocket 客户端，用于手工测试和 make demo")
    parser.add_argument("--token", required=True)
    parser.add_argument("--conversation-id", required=True)
    parser.add_argument("--content", required=True)
    parser.add_argument("--message-id", default=None, help="不传就自动生成一个新的；重发演示去重时传上一次用过的")
    parser.add_argument("--gateway-url", default="ws://gateway:8000/ws", help="默认走 docker 网络内部地址")
    args = parser.parse_args()

    message_id = args.message_id or str(uuid.uuid4())
    asyncio.run(run(args.token, args.conversation_id, args.content, message_id, args.gateway_url))


if __name__ == "__main__":
    main()
