"""mock-llm：OpenAI 兼容的假 LLM 服务。

行为通过环境变量给默认值，/admin/config 可以运行时覆盖，方便演示"LLM 变慢/报错"这类故障场景，
不用重启容器、不用改代码。工具调用走确定性规则（见 rules.py），保证测试可复现。
"""
import asyncio
import json
import os
import random
import time
import uuid
from typing import Any, AsyncIterator, List, Literal, Optional, Union

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from mocks.mock_llm.rules import extract_first_material, is_handoff_summary_request, match_tool_call

app = FastAPI(title="mock-llm")

# 用一个进程内可变字典存行为配置：启动时读环境变量做默认值，/admin/config 之后可以随时改
_config: dict = {
    "latency_ms": int(os.getenv("MOCK_LLM_LATENCY_MS", "300")),
    "error_rate": float(os.getenv("MOCK_LLM_ERROR_RATE", "0.0")),
    "mode": os.getenv("MOCK_LLM_MODE", "normal"),
}
# 启动时的快照，/admin/reset 用来把 _config 恢复到这个状态（给 mockctl.py all reset 用）
_DEFAULT_CONFIG: dict = dict(_config)

_DEFAULT_REPLY = "好的，我在。你可以直接说你的问题。"
_HALLUCINATE_PREFIX = "根据《课程服务协议》第 9.9 条，所有课程都可以随时全额退款。"
_AI_FLAVOR_SUFFIX = "希望对你有帮助！"
_HANDOFF_SUMMARY_REPLY = "用户咨询的问题已按流程处理，暂无异常情况。建议人工核实后继续跟进。"


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str
    messages: List[ChatMessage]
    stream: bool = False
    tools: Optional[List[dict]] = None
    tool_choice: Optional[Union[str, dict]] = None


class AdminConfigUpdate(BaseModel):
    latency_ms: Optional[int] = Field(default=None, ge=0)
    error_rate: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    mode: Optional[Literal["normal", "hallucinate", "invalid_json", "ai_flavor", "error500"]] = None


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/admin/config")
async def get_config() -> dict:
    return _config


@app.post("/admin/config")
async def update_config(update: AdminConfigUpdate) -> dict:
    if update.latency_ms is not None:
        _config["latency_ms"] = update.latency_ms
    if update.error_rate is not None:
        _config["error_rate"] = update.error_rate
    if update.mode is not None:
        _config["mode"] = update.mode
    return _config


@app.post("/admin/reset")
async def reset_config() -> dict:
    _config.update(_DEFAULT_CONFIG)
    return _config


def _maybe_raise_error() -> None:
    if _config["mode"] == "error500":
        # error500 是确定性故障模式，跟 error_rate 的概率性故障不是一回事：
        # error_rate 用来测"偶尔失败会不会拖垮系统"，error500 用来测"这一次一定失败会怎样"
        raise HTTPException(status_code=500, detail="mock-llm 模拟的上游错误（error500 模式）")
    if random.random() < _config["error_rate"]:
        raise HTTPException(status_code=500, detail="mock-llm 模拟的上游错误")


def _last_user_content(messages: List[ChatMessage]) -> str:
    for msg in reversed(messages):
        if msg.role == "user":
            return msg.content
    return messages[-1].content if messages else ""


def _build_text_reply(messages: List[ChatMessage]) -> str:
    """请求不带 tools 时的文字生成（阶段二 2.5 第 2 点）"""
    content = _last_user_content(messages)

    material = extract_first_material(content)
    if material is not None:
        reply = f"我查到的规定是：{material}"
    elif is_handoff_summary_request(content):
        reply = _HANDOFF_SUMMARY_REPLY
    else:
        reply = _DEFAULT_REPLY

    if _config["mode"] == "hallucinate":
        reply = _HALLUCINATE_PREFIX + reply
    elif _config["mode"] == "ai_flavor":
        reply = reply + _AI_FLAVOR_SUFFIX
    return reply


def _tool_call_arguments(name: str, args: dict) -> str:
    arguments = json.dumps(args, ensure_ascii=False)
    if _config["mode"] == "invalid_json":
        # 截断成坏 JSON：切掉结尾几个字符，保证解析一定失败，而不是随便拼一个固定的坏字符串
        arguments = arguments[:-3]
    return arguments


async def _stream_text_chunks(reply: str, completion_id: str, model: str) -> AsyncIterator[str]:
    await asyncio.sleep(_config["latency_ms"] / 1000)
    created = int(time.time())
    for ch in reply:
        chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"content": ch}, "finish_reason": None}],
        }
        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
        await asyncio.sleep(0.03)  # 逐字输出的间隔，只是为了演示打字机效果，不用配置化
    end_chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(end_chunk, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"


async def _stream_tool_call_chunks(
    name: str, arguments: str, completion_id: str, model: str
) -> AsyncIterator[str]:
    await asyncio.sleep(_config["latency_ms"] / 1000)
    created = int(time.time())
    # 简化处理：一次性把整个 tool_call 放进一个 delta（不像文字那样逐字拆），
    # OpenAI 客户端按 tool_calls[].index 累积增量，单块也能正确解析
    chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": f"call_{uuid.uuid4().hex[:24]}",
                            "type": "function",
                            "function": {"name": name, "arguments": arguments},
                        }
                    ]
                },
                "finish_reason": None,
            }
        ],
    }
    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
    end_chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
    }
    yield f"data: {json.dumps(end_chunk, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"


def _usage(messages: List[ChatMessage], completion_text: str) -> dict:
    # 按字数估算 token，不用真的接分词器——只是给 NFR-5 token 统计留一个数值来源
    prompt_tokens = sum(len(m.content) for m in messages)
    completion_tokens = len(completion_text)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest):
    _maybe_raise_error()
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"

    tool_call: Optional[tuple[str, dict]] = None
    if req.tools:
        content = _last_user_content(req.messages)
        tool_call = match_tool_call(content)

    if tool_call is not None:
        name, args = tool_call
        arguments = _tool_call_arguments(name, args)

        if req.stream:
            return StreamingResponse(
                _stream_tool_call_chunks(name, arguments, completion_id, req.model),
                media_type="text/event-stream",
            )

        await asyncio.sleep(_config["latency_ms"] / 1000)
        return {
            "id": completion_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": req.model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": f"call_{uuid.uuid4().hex[:24]}",
                                "type": "function",
                                "function": {"name": name, "arguments": arguments},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": _usage(req.messages, arguments),
        }

    # 没带 tools，或者带了 tools 但规则没命中任何一条（R5：闲聊）——都走文字生成
    reply = _build_text_reply(req.messages)

    if req.stream:
        return StreamingResponse(
            _stream_text_chunks(reply, completion_id, req.model),
            media_type="text/event-stream",
        )

    await asyncio.sleep(_config["latency_ms"] / 1000)
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": req.model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": reply},
                "finish_reason": "stop",
            }
        ],
        "usage": _usage(req.messages, reply),
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
