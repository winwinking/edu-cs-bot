"""题目 6.3 场景 4：用户关闭自动续费，机器人二次确认后执行。

三件事：第一条回复只问确认、不执行；回复"确认关闭"后才真的执行，且 PendingAction 落库状态是
executed；mock-platform 只被真正调用了一次（幂等——不会因为重复确认或重试就关两次）。

先 reset mock-platform：u_a_1001 的"春季数学班"默认是开着自动续费的种子状态，如果之前跑过
scripts/phase2_smoke.py 或本文件自己，这门课可能已经被关掉，导致这次直接落进"已经是关闭状态，
不用重复操作"分支，测不出真正的确认流程。

【人工审查发现，检查点 E 修复】原来的第 4 点断言是自己拼一份跟 `app/worker/graph/command.py`
里完全相同的 `idempotency_key` 格式（`{tenant_id}:{conversation_id}:{action}:{pending_id}`）
去精确匹配，等于把业务代码生成幂等键的公式抄了一遍到测试里：如果业务代码这个公式本身有 bug
（比如漏掉某个字段导致不够唯一），测试和业务代码用的是同一个（有问题的）公式，测试永远看不出
问题；而且原来只制造了一次确认，测不出"重复确认会不会重复执行"。改成直接数
`(tenant_id, user_id, action)` 这三个维度过滤出的指令条数，确认前后比差值——不管业务代码内部
怎么生成 key，只要"真正执行的次数"不对，测试就会失败；并且在第一次确认成功之后再发一次
"确认关闭"，验证条数还是只多 1，不会因为重复确认就多关一次。
"""
import uuid

import httpx
import pytest
from sqlalchemy import select

from app.common.config import get_settings
from app.common.db import AsyncSessionLocal
from app.common.models import PendingAction, PendingActionStatus

from tests.e2e._helpers import conversation_id, reset_mock, send_and_wait

settings = get_settings()


async def _platform_commands() -> list[dict]:
    async with httpx.AsyncClient(timeout=5) as client:
        resp = await client.get("http://mock-platform:8000/admin/commands")
        resp.raise_for_status()
        return resp.json()["commands"]


def _count_disable_auto_renew_commands(commands: list[dict], *, tenant_id: str, user_id: str) -> int:
    return sum(
        1
        for c in commands
        if c["tenant_id"] == tenant_id and c["user_id"] == user_id and c["action"] == "disable_auto_renew"
    )


@pytest.mark.asyncio
async def test_disable_auto_renew_requires_confirmation_then_executes_exactly_once():
    await reset_mock("platform")
    conv_label = f"e2e_s4_{uuid.uuid4().hex[:8]}"
    conv_id = conversation_id("t_a", "u_a_1001", conv_label)
    tenant_id, user_id = "t_a", "u_a_1001"

    # 1) 第一条回复：只问确认，不执行
    r1 = await send_and_wait(tenant_id, user_id, conv_label, "帮我把自动续费关了")
    assert "确认一下" in r1["reply"]
    assert "确认关闭" in r1["reply"]
    pending_id = r1["meta"]["pending_action_id"]
    assert pending_id is not None

    count_before = _count_disable_auto_renew_commands(
        await _platform_commands(), tenant_id=tenant_id, user_id=user_id
    )
    assert count_before == 0  # reset_mock 之后、确认之前，不该有任何一次真正执行

    # 2) 回复"确认关闭"后才执行
    r2 = await send_and_wait(tenant_id, user_id, conv_label, "确认关闭")
    assert "已关闭" in r2["reply"] and "春季数学班" in r2["reply"]

    # 3) 数据库状态：PendingAction 变成 executed
    async with AsyncSessionLocal() as session:
        pending = (
            await session.execute(select(PendingAction).where(PendingAction.id == uuid.UUID(pending_id)))
        ).scalar_one()
    assert pending.status == PendingActionStatus.executed

    # 4) mock-platform 只真正执行了一次：不重建 idempotency_key，直接数这个用户这个动作的
    # 指令条数，确认前后差值等于 1
    count_after_first_confirm = _count_disable_auto_renew_commands(
        await _platform_commands(), tenant_id=tenant_id, user_id=user_id
    )
    assert count_after_first_confirm - count_before == 1

    # 5) 幂等的关键场景：同一个待确认操作被确认了两次（比如用户手快连发两条"确认关闭"，或者
    # 客户端重试），第二次不应该再真正执行一次。第二次的回复内容不做断言（PLATFORM_ALREADY_
    # PROCESSED_REPLY 之类都算合理），只贴出来给 Jo 看
    r3 = await send_and_wait(tenant_id, user_id, conv_label, "确认关闭")
    print(f"[场景4] 第二次'确认关闭'的回复：{r3['reply']!r}")

    count_after_second_confirm = _count_disable_auto_renew_commands(
        await _platform_commands(), tenant_id=tenant_id, user_id=user_id
    )
    assert count_after_second_confirm - count_before == 1
