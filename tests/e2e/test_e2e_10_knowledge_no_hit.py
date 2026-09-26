"""题目 6.3 场景 10：知识库无命中，机器人不瞎编，建议转人工，没有调 LLM 生成。

三件事：回复是固定的"没有查到明确依据"话术；meta 里 citations 为空、search_knowledge 状态
正常返回（"无命中"不是检索失败，是真的没有匹配到超过阈值的条款）；respond 阶段耗时极短，
证明走的是 app/worker/graph/knowledge.py 里 `reply_plan.mode="template"` 的直出话术分支，
没有再调一次 LLM 生成——如果真的调了 LLM 生成，respond 耗时会是几百到几千毫秒量级
（对照场景 1 命中知识库时 respond 耗时通常 >1000ms），不是这里的量级。
"""
import uuid

import pytest

from app.worker.graph.style import KNOWLEDGE_NO_HIT_REPLY

from tests.e2e._helpers import send_and_wait

# respond 节点如果真的调了 LLM 流式生成，耗时不可能低于这个值（意图识别本身都要 ~300ms，
# 生成还要再叠加首字延迟 + 逐字吐字时间）；模板直出通常在几毫秒量级
_TEMPLATE_RESPOND_MS_CEILING = 200


@pytest.mark.asyncio
async def test_knowledge_no_hit_does_not_fabricate_and_skips_llm_generation():
    conv_label = f"e2e_s10_{uuid.uuid4().hex[:8]}"

    result = await send_and_wait("t_a", "u_a_1001", conv_label, "你们的校车几点发车？")

    # 1) 回复内容：固定话术，不是瞎编的答案
    assert result["reply"] == KNOWLEDGE_NO_HIT_REPLY

    # 2) meta：没有命中任何条款
    assert result["meta"]["citations"] == []
    tool_status = next(t["status"] for t in result["meta"]["tools"] if t["name"] == "search_knowledge")
    assert tool_status == "ok"  # 检索本身正常执行，只是没有超过阈值的结果，不是检索失败

    # 3) 没有调 LLM 生成：respond 阶段耗时远低于真的生成一次要花的时间
    assert result["meta"]["timings"]["respond"] < _TEMPLATE_RESPOND_MS_CEILING
