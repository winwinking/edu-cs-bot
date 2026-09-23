"""AsyncOpenAI 封装。base_url/api_key/model/timeout 全部来自配置，
切换 mock-llm 和 DeepSeek 只改 .env 里的 LLM_* 变量，业务代码不用动。
"""
from typing import AsyncIterator, Iterable, Mapping

from openai import AsyncOpenAI

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
