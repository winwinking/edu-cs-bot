"""覆盖题目 6.2 点名的"LLM mock：调用 mock-llm 拿到结构化的工具调用"。

直接用 app.common.llm_client.chat_completion 打真实网络请求到 mock-llm 容器（不 mock 这一层），
再喂给 app.common.tools.parse_tool_call 做 JSON Schema 校验——这条链路跟 worker 的 classify
节点走的是同一份代码，验证"LLM 返回的工具调用"和"我们自己的校验层"真的对得上，不是意图识别
本身要不要用 LLM 这类业务判断（那属于 tests/unit 覆盖的范围）。
"""
import pytest

from app.common.llm_client import chat_completion
from app.common.tools import ParsedToolCall, parse_tool_call, to_openai_tools


@pytest.mark.asyncio
async def test_mock_llm_returns_structured_tool_call_for_finance_question():
    response = await chat_completion(
        messages=[{"role": "user", "content": "我上个月的发票开了吗"}],
        tools=to_openai_tools(),
        tool_choice="auto",
    )

    tool_calls = response.choices[0].message.tool_calls
    assert tool_calls, "mock-llm 应该识别出这是一次工具调用，不是纯文本回复"

    call = tool_calls[0]
    parsed = parse_tool_call(call.function.name, call.function.arguments)

    assert isinstance(parsed, ParsedToolCall)
    assert parsed.name == "query_finance"
    assert parsed.args.kind == "invoices"
    assert parsed.args.period == "last_month"


@pytest.mark.asyncio
async def test_mock_llm_returns_structured_tool_call_for_platform_command():
    response = await chat_completion(
        messages=[{"role": "user", "content": "帮我把自动续费关了"}],
        tools=to_openai_tools(),
        tool_choice="auto",
    )

    call = response.choices[0].message.tool_calls[0]
    parsed = parse_tool_call(call.function.name, call.function.arguments)

    assert isinstance(parsed, ParsedToolCall)
    assert parsed.name == "platform_command"
    assert parsed.args.action == "disable_auto_renew"
    assert parsed.is_high_risk is True  # 关自动续费是高风险指令，必须走二次确认
