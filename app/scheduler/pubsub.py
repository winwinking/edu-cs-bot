"""scheduler 往 gateway 订阅的 Redis 频道推提醒消息。跟 worker（app/worker/pubsub.py）推对话
回复走同一个频道命名规则（im:out:{tenant_id}:{user_id}）、同一条 gateway 转发链路，但消息类型
不同（type="reminder"），gateway 本身不关心这个区别，只管原样转发（见 app/gateway/connection_manager.py）。
"""
from app.common.redis import redis_client
from app.common.schemas import ReminderPushMessage


def _channel(tenant_id: str, user_id: str) -> str:
    return f"im:out:{tenant_id}:{user_id}"


async def publish_reminder_push(tenant_id: str, user_id: str, reminder_id: str, title: str, text: str) -> None:
    msg = ReminderPushMessage(reminder_id=reminder_id, title=title, text=text)
    await redis_client.publish(_channel(tenant_id, user_id), msg.model_dump_json())
