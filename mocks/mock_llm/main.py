"""mock-llm：OpenAI 兼容的假 LLM 服务。

行为通过环境变量给默认值，/admin/config 可以运行时覆盖，方便演示"LLM 变慢/报错"这类故障场景，
不用重启容器、不用改代码。
"""
import asyncio
import json
import os
import random
import time
import uuid
from typing import AsyncIterator, List, Literal, Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

app = FastAPI(title="mock-llm")

# 用一个进程内可变字典存行为配置：启动时读环境变量做默认值，/admin/config 之后可以随时改
_config: dict = {
    "latency_ms": int(os.getenv("MOCK_LLM_LATENCY_MS", "300")),
    "error_rate": float(os.getenv("MOCK_LLM_ERROR_RATE", "0.0")),
    "mode": os.getenv("MOCK_LLM_MODE", "normal"),
}
# 启动时的快照，/admin/reset 用来把 _config 恢复到这个状态（给 mockctl.py all reset 用）
_DEFAULT_CONFIG: dict = dict(_config)

_REPLY_TEMPLATE = (
    "您好，我已经收到您的问题，正在为您查询处理，请稍等。"
    "根据目前掌握的信息，建议您可以先查看课程详情页，或者联系人工客服获取更详细的帮助。"
    "如果还有其他问题，请随时告诉我。"
)


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str
    messages: List[ChatMessage]
    stream: bool = False


class AdminConfigUpdate(BaseModel):
    latency_ms: Optional[int] = Field(default=None, ge=0)
    error_rate: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    # hallucinate/invalid_json 先占位，阶段四做故障注入时再实现对应行为，本阶段按 normal 处理
    mode: Optional[Literal["normal", "hallucinate", "invalid_json"]] = None


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
    if random.random() < _config["error_rate"]:
        raise HTTPException(status_code=500, detail="mock-llm 模拟的上游错误")


async def _stream_chunks(reply: str, completion_id: str, model: str) -> AsyncIterator[str]:
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


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest):
    _maybe_raise_error()
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    reply = _REPLY_TEMPLATE

    if req.stream:
        return StreamingResponse(
            _stream_chunks(reply, completion_id, req.model),
            media_type="text/event-stream",
        )

    await asyncio.sleep(_config["latency_ms"] / 1000)
    prompt_tokens = sum(len(m.content) for m in req.messages)
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
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": len(reply),
            "total_tokens": prompt_tokens + len(reply),
        },
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
