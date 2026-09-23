#!/bin/sh
# make demo 跑这个：自动生成 token -> 发一条消息看完整链路和三个耗时 -> 用同一个 message_id 重发看 duplicate
set -eu

TENANT=${DEMO_TENANT:-t_a}
USER=${DEMO_USER:-u_a_1001}
CONVERSATION_ID=$(python -c "import uuid; print(uuid.uuid4())")
CONTENT="你好，我想咨询一下课程安排"
LOG_FILE=$(mktemp)

echo "=== 1. 生成 token（tenant=$TENANT user=$USER）==="
TOKEN=$(python scripts/gen_token.py --tenant "$TENANT" --user "$USER")
echo "token: $TOKEN"

echo
echo "=== 2. 发送第一条消息，展示 ACK / 首 token / 完整回复三个耗时 ==="
python scripts/ws_client.py --token "$TOKEN" --conversation-id "$CONVERSATION_ID" --content "$CONTENT" | tee "$LOG_FILE"
MESSAGE_ID=$(grep '^MESSAGE_ID=' "$LOG_FILE" | head -1 | cut -d= -f2)

echo
echo "=== 3. 用同一个 message_id 重发，展示 duplicate（不会再触发一次新回复）==="
python scripts/ws_client.py --token "$TOKEN" --conversation-id "$CONVERSATION_ID" --content "$CONTENT" --message-id "$MESSAGE_ID"

rm -f "$LOG_FILE"
