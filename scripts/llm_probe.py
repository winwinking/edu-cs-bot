"""带上完整工具列表请求一次 LLM，打印 tool_calls 原文（阶段二 2.5）。

工具的 JSON Schema 是这里临时手写的最小版本，只是用来触发 mock-llm 的规则引擎；
2.6 会有一份从 Pydantic 模型生成的正式工具注册表，那时候 worker 用的是那一份，
不是这里手写的这份。这个脚本只是 mock-llm 规则的独立探针，不接业务代码。

用法：python scripts/llm_probe.py "帮我把自动续费关了"
"""
import argparse
import asyncio
import json

from app.common.llm_client import LLM_MODEL, llm_client

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_knowledge",
            "description": "检索知识库回答政策类问题",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_finance",
            "description": "查询财务信息（订单/账单/发票/退费/余额）",
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["orders", "bills", "invoices", "refunds", "balance"]},
                    "period": {"type": "string"},
                    "target_user_id": {"type": "string"},
                },
                "required": ["kind"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "platform_command",
            "description": "执行平台指令（自动续费、请假、课程表等）",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string"},
                    "course_name": {"type": "string"},
                    "date": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manage_reminder",
            "description": "创建/修改/取消日程提醒",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["create", "update", "cancel"]},
                    "raw_text": {"type": "string"},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "transfer_to_human",
            "description": "转接人工客服",
            "parameters": {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
            },
        },
    },
]


async def main(query: str) -> None:
    response = await llm_client.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": query}],
        tools=TOOLS,
        tool_choice="auto",
    )
    message = response.choices[0].message

    if not message.tool_calls:
        print(f"[无工具调用] content={message.content!r}")
        return

    for call in message.tool_calls:
        print(f"[tool_call] name={call.function.name}")
        print(f"  arguments(原文)={call.function.arguments!r}")
        try:
            print(f"  arguments(解析后)={json.loads(call.function.arguments)}")
        except json.JSONDecodeError as exc:
            print(f"  arguments 不是合法 JSON：{exc}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="探测 mock-llm 的 tool_calls 规则")
    parser.add_argument("query", help="用户消息内容")
    args = parser.parse_args()
    asyncio.run(main(args.query))
