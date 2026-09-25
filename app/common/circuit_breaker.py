"""熔断器（PHASE3.md 第 5 步，设计决定 10）。

状态放在这个进程自己的内存里，不放 Redis：Redis 本身也可能挂，状态放它那里等于给熔断器又
挂了一个可能失败的外部依赖；实现也更简单。代价是开多个 worker 时，每个 worker 各自失败几次
才会各自熔断，互相不知道对方的状态（已知问题，写进 AGENT_LOG）。

三态：closed（正常）-> open（连续失败到阈值，直接拒绝请求）-> half_open（熔断到时间，放一个
试探请求）-> 试探成功回 closed / 试探失败回 open 重新计时。
"""
import time
from dataclasses import dataclass, field

from prometheus_client import Gauge

from app.common.logging import get_logger

logger = get_logger(__name__)

# 0=closed 1=half_open 2=open（阶段三第 6 步）：数值化是因为 Prometheus Gauge 只能存数字，
# 具体含义只在这三个值之间切换，画图/告警时按数值区间判断就行，不需要额外的映射表
_STATE_VALUE = {"closed": 0, "half_open": 1, "open": 2}
circuit_breaker_state = Gauge("worker_circuit_breaker_state", "熔断器状态：0=closed 1=half_open 2=open", ["service"])


class CircuitBreakerOpenError(Exception):
    """熔断打开（或试探名额已经被占用）期间调用方不应该真的发请求，直接抛这个异常，
    调用方按"这个服务暂时不可用"处理，走跟真实超时/5xx 一样的降级路径。"""

    def __init__(self, service: str) -> None:
        super().__init__(f"熔断打开：{service}")
        self.service = service


@dataclass
class CircuitBreaker:
    name: str
    failure_threshold: int
    open_seconds: float

    _state: str = field(default="closed", init=False)
    _failure_count: int = field(default=0, init=False)
    _opened_at: float = field(default=0.0, init=False)
    # half_open 时只放一个试探请求，其余并发请求在试探结果出来之前一律当成还在熔断
    _probing: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        circuit_breaker_state.labels(service=self.name).set(_STATE_VALUE[self._state])

    def _set_state(self, new_state: str) -> None:
        self._state = new_state
        circuit_breaker_state.labels(service=self.name).set(_STATE_VALUE[new_state])

    def _refresh_state(self) -> None:
        if self._state == "open" and time.monotonic() - self._opened_at >= self.open_seconds:
            self._set_state("half_open")
            logger.info("熔断进入半开，等待试探请求", service=self.name)

    @property
    def state(self) -> str:
        self._refresh_state()
        return self._state

    def allow_request(self) -> bool:
        self._refresh_state()
        if self._state == "closed":
            return True
        if self._state == "half_open" and not self._probing:
            self._probing = True
            return True
        return False

    def record_success(self) -> None:
        if self._state != "closed":
            logger.info("熔断恢复", service=self.name)
        self._set_state("closed")
        self._failure_count = 0
        self._probing = False

    def record_failure(self) -> None:
        self._probing = False
        if self._state == "half_open":
            self._set_state("open")
            self._opened_at = time.monotonic()
            logger.warning("熔断试探请求失败，继续熔断", service=self.name)
            return
        self._failure_count += 1
        if self._failure_count >= self.failure_threshold and self._state != "open":
            self._set_state("open")
            self._opened_at = time.monotonic()
            logger.warning("连续失败达到阈值，熔断打开", service=self.name, failures=self._failure_count)
