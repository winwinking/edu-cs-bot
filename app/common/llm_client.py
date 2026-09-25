"""AsyncOpenAI 封装。base_url/api_key/model/timeout 全部来自配置，
切换 mock-llm 和 DeepSeek 只改 .env 里的 LLM_* 变量，业务代码不用动。

熔断 + 重试（PHASE3.md 第 5 步，设计决定 10、11）：
- `max_retries=0` 关掉 OpenAI SDK 自带的重试——不关掉的话 SDK 自己会重试几次，我们这里又重试
  一次，一次调用变成好几次，退避时间也对不上我们自己配的值。
- 重试只处理超时和 5xx，只重试 1 次（次数和退避间隔见 Settings），4xx 之类的业务错误重试没用。
- 熔断器状态在这个模块里维护成单例：LLM 调用不管走 chat_completion 还是 stream_chat_completion，
  都是同一个服务、共用同一个熔断状态。
"""
import asyncio
from typing import Any, AsyncIterator, Iterable, Mapping, Optional, Union

from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError, AsyncOpenAI
from openai.types.chat import ChatCompletion
from prometheus_client import Counter

from app.common.circuit_breaker import CircuitBreaker, CircuitBreakerOpenError
from app.common.config import get_settings
from app.common.logging import get_logger

settings = get_settings()
logger = get_logger(__name__)

# 按结果分：ok / error（重试用完还是失败）/ circuit_open（熔断打开，根本没真的发请求）——
# 跟 worker_messages_total 的 llm_degraded 标签不是一回事，这个是"调 LLM 这一步本身"的计数，
# 一条用户消息处理过程中可能调 0~2 次 LLM（意图识别一次、生成回复可能再一次），阶段三第 6 步。
# 定义放在这个模块本身（而不是 app/worker/metrics.py）：app/common 不应该反过来依赖 app/worker，
# Prometheus 的默认注册表是进程全局的，worker 进程只要 import 了这个模块，/metrics 就能看到它
llm_requests_total = Counter("worker_llm_requests_total", "调用 LLM 次数，按结果分", ["result"])

llm_client = AsyncOpenAI(
    base_url=settings.llm_base_url,
    api_key=settings.llm_api_key,
    timeout=settings.llm_timeout_seconds,
    max_retries=0,
)

LLM_MODEL = settings.llm_model

_breaker = CircuitBreaker(
    name="llm", failure_threshold=settings.cb_failure_threshold, open_seconds=settings.cb_open_seconds
)


def llm_circuit_state() -> str:
    """给指标/排障用，不参与业务判断"""
    return _breaker.state


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, APIStatusError):
        return exc.status_code >= 500
    # APITimeoutError 是 APIConnectionError 的子类，isinstance 判断顺序不影响结果
    return isinstance(exc, (APITimeoutError, APIConnectionError))


def estimate_tokens(messages: Iterable[Mapping[str, str]], completion_text: str) -> tuple[int, int]:
    """真实 usage 拿不到时的兜底估算：按字数算，跟 mock-llm 自己估算 usage 用的口径一样
    （见 mocks/mock_llm/main.py 的 `_usage()`），不是真的接分词器——只是给 token 统计留一个
    数值来源，调用方要把这种情况标成 estimated=True，不能当成精确成本汇总。"""
    prompt_tokens = sum(len(m.get("content") or "") for m in messages)
    completion_tokens = len(completion_text)
    return prompt_tokens, completion_tokens


def extract_usage_from_response(response: ChatCompletion) -> Optional[tuple[int, int]]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    return usage.prompt_tokens, usage.completion_tokens


async def stream_chat_completion(
    messages: Iterable[Mapping[str, str]], *, usage_holder: Optional[dict[str, int]] = None
) -> AsyncIterator[str]:
    """流式请求 LLM，逐个 yield 文本分片；调用方不用直接碰 OpenAI SDK。

    只重试"还没吐出任何内容"的失败：一旦开始迭代到 chunk（哪怕内容是空字符串），说明请求本身
    已经打通了，后面的失败留给调用方（app/worker/graph/graph.py 的 respond()）按"部分内容已经
    发出去了"处理，这里再重试会导致同一句话被生成/发送两遍。

    `usage_holder`（阶段三第 6 步）：流式响应是逐块 yield 文本的生成器，没法像非流式调用那样
    直接 return 一个带 usage 字段的对象，所以改成调用方传一个字典进来、由这里原地写入
    prompt_tokens/completion_tokens——用法跟 GraphState 里 circuit_breaker 字段"节点原地追加"
    是同一个思路。请求里带 `stream_options={"include_usage": True}`，真实 usage 拿不到（提供方
    不支持，或者流提前中断）时，调用方看 usage_holder 是不是空字典来判断要不要退回估算。
    """
    if not _breaker.allow_request():
        llm_requests_total.labels(result="circuit_open").inc()
        raise CircuitBreakerOpenError("llm")

    message_list = list(messages)
    last_exc: Optional[BaseException] = None
    for attempt in range(settings.llm_max_retries + 1):
        if attempt > 0:
            await asyncio.sleep(settings.llm_retry_backoff_seconds)
        try:
            stream = await llm_client.chat.completions.create(
                model=LLM_MODEL,
                messages=message_list,
                stream=True,
                stream_options={"include_usage": True},
            )
        except (APIError, APIConnectionError, APITimeoutError) as exc:
            last_exc = exc
            if not _is_retryable(exc) or attempt == settings.llm_max_retries:
                _breaker.record_failure()
                llm_requests_total.labels(result="error").inc()
                raise
            logger.warning("LLM 流式请求建立失败，重试", attempt=attempt + 1, error=str(exc))
            continue

        try:
            async for chunk in stream:
                if chunk.usage is not None and usage_holder is not None:
                    usage_holder["prompt_tokens"] = chunk.usage.prompt_tokens
                    usage_holder["completion_tokens"] = chunk.usage.completion_tokens
                if not chunk.choices:
                    continue  # 只带 usage、不带正文的收尾 chunk
                delta = chunk.choices[0].delta.content
                if delta:
                    yield delta
            _breaker.record_success()
            llm_requests_total.labels(result="ok").inc()
            return
        except (APIError, APIConnectionError, APITimeoutError):
            # 流已经建立、内容已经开始往外吐，不再重试，交给调用方处理部分内容的情况
            _breaker.record_failure()
            llm_requests_total.labels(result="error").inc()
            raise
    raise last_exc  # 正常不会走到这里，保留是为了类型检查器满意


async def chat_completion(
    messages: Iterable[Mapping[str, str]],
    *,
    tools: Optional[list[dict[str, Any]]] = None,
    tool_choice: Optional[Union[str, dict[str, Any]]] = None,
) -> ChatCompletion:
    """非流式请求 LLM，给 2.7 的 classify 节点判断意图用——分类只要最终结果，不用一边判断一边流式吐字。"""
    if not _breaker.allow_request():
        llm_requests_total.labels(result="circuit_open").inc()
        raise CircuitBreakerOpenError("llm")

    kwargs: dict[str, Any] = {"model": LLM_MODEL, "messages": list(messages), "stream": False}
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = tool_choice or "auto"

    last_exc: Optional[BaseException] = None
    for attempt in range(settings.llm_max_retries + 1):
        if attempt > 0:
            await asyncio.sleep(settings.llm_retry_backoff_seconds)
        try:
            result = await llm_client.chat.completions.create(**kwargs)
        except (APIError, APIConnectionError, APITimeoutError) as exc:
            last_exc = exc
            if not _is_retryable(exc) or attempt == settings.llm_max_retries:
                _breaker.record_failure()
                llm_requests_total.labels(result="error").inc()
                raise
            logger.warning("LLM 调用失败，重试", attempt=attempt + 1, error=str(exc))
            continue
        _breaker.record_success()
        llm_requests_total.labels(result="ok").inc()
        return result
    raise last_exc  # 正常不会走到这里，保留是为了类型检查器满意
