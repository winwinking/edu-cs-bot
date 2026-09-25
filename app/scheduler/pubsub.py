"""scheduler 往 gateway 订阅的 Redis 频道推提醒消息。跟 worker（app/worker/pubsub.py）推对话
回复走同一个频道命名规则（im:out:{tenant_id}:{user_id}）、同一条 gateway 转发链路，但消息类型
不同（type="reminder"），gateway 本身不关心这个区别，只管原样转发（见 app/gateway/connection_manager.py）。

Redis 发布失败时往外抛异常（跟 worker 那边不一样）：调用方 app/scheduler/main.py 按设计决定 4
"先推送再提交"，需要知道这一条到底有没有推成功，推失败就不更新 next_trigger_at、不提交，
下一秒重试，不能在这里悄悄吞掉。
"""
from app.common.redis import note_redis_result, redis_client
from app.common.schemas import ReminderPushMessage


def _channel(tenant_id: str, user_id: str) -> str:
    return f"im:out:{tenant_id}:{user_id}"


async def publish_reminder_push(tenant_id: str, user_id: str, reminder_id: str, title: str, text: str) -> None:
    msg = ReminderPushMessage(reminder_id=reminder_id, title=title, text=text)
    try:
        await redis_client.publish(_channel(tenant_id, user_id), msg.model_dump_json())
    except Exception:
        note_redis_result(False)
        raise
    note_redis_result(True)
