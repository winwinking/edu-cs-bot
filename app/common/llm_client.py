"""AsyncOpenAI 封装。base_url/api_key/model/timeout 全部来自配置，
切换 mock-llm 和 DeepSeek 只改 .env 里的 LLM_* 变量，业务代码不用动。
"""
from typing import Any, AsyncIterator, Iterable, Mapping, Optional, Union

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion

from app.common.config import get_settings

settings = get_settings()

llm_client = AsyncOpenAI(
    base_url=settings.llm_base_url,
    api_key=settings.llm_api_key,
    timeout=settings.llm_timeout_seconds,
)

LLM_MODEL = settings.llm_model


async def stream_chat_completion(messages: Iterable[Mapping[str, str]]) -> AsyncIterator[str]:
    """流式请求 LLM，逐个 yield 文本分片；调用方不用直接碰 OpenAI SDK"""
    stream = await llm_client.chat.completions.create(
        model=LLM_MODEL,
        messages=list(messages),
        stream=True,
    )
    async for chunk in stream:
        delta = chunk.choices[0].delta.content
        if delta:
            yield delta


async def chat_completion(
    messages: Iterable[Mapping[str, str]],
    *,
    tools: Optional[list[dict[str, Any]]] = None,
    tool_choice: Optional[Union[str, dict[str, Any]]] = None,
) -> ChatCompletion:
    """非流式请求 LLM，给 2.7 的 classify 节点判断意图用——分类只要最终结果，不用一边判断一边流式吐字。

    超时沿用建 client 时传的 LLM_TIMEOUT_SECONDS；OpenAI SDK 自带有限次数重试，不需要再包一层。
    """
    kwargs: dict[str, Any] = {"model": LLM_MODEL, "messages": list(messages), "stream": False}
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = tool_choice or "auto"
    return await llm_client.chat.completions.create(**kwargs)
