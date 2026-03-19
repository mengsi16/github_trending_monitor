"""Agent 级别 Circuit Breaker - 防止连续失败"""
import time
import threading
from enum import Enum
from typing import Callable, Any, Optional
from dataclasses import dataclass, field


class CircuitState(Enum):
    """熔断器状态"""
    CLOSED = "closed"      # 正常状态
    OPEN = "open"         # 熔断状态
    HALF_OPEN = "half_open"  # 半开状态（尝试恢复）


@dataclass
class CircuitBreakerConfig:
    """熔断器配置"""
    failure_threshold: int = 5       # 连续失败多少次后熔断
    success_threshold: int = 2       # 半开状态下成功多少次后关闭
    timeout: float = 60.0            # 熔断持续时间（秒）
    half_open_max_calls: int = 3     # 半开状态下的最大尝试次数


@dataclass
class CircuitStats:
    """熔断器统计"""
    total_calls: int = 0
    successful_calls: int = 0
    failed_calls: int = 0
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    last_failure_time: Optional[float] = None
    last_failure_error: Optional[str] = None
    state: CircuitState = CircuitState.CLOSED
    state_history: list = field(default_factory=list)


class CircuitBreaker:
    """
    Circuit Breaker 实现

    状态转换:
    - CLOSED -> OPEN: 连续失败达到阈值
    - OPEN -> HALF_OPEN: 超时后
    - HALF_OPEN -> CLOSED: 连续成功达到阈值
    - HALF_OPEN -> OPEN: 任何失败
    """

    def __init__(self, name: str, config: CircuitBreakerConfig = None):
        self.name = name
        self.config = config or CircuitBreakerConfig()
        self._state = CircuitState.CLOSED
        self._stats = CircuitStats()
        self._lock = threading.RLock()
        self._last_state_change = time.time()
        self._half_open_calls = 0

    @property
    def state(self) -> CircuitState:
        """获取当前状态"""
        with self._lock:
            # 检查是否需要从 OPEN 转换到 HALF_OPEN
            if self._state == CircuitState.OPEN:
                if time.time() - self._last_state_change >= self.config.timeout:
                    self._transition_to(CircuitState.HALF_OPEN)
            return self._state

    def _transition_to(self, new_state: CircuitState) -> None:
        """状态转换"""
        old_state = self._state
        self._state = new_state
        self._last_state_change = time.time()

        # 记录状态历史
        self._stats.state_history.append({
            "from": old_state.value,
            "to": new_state.value,
            "timestamp": self._last_state_change
        })

        print(f"[CircuitBreaker:{self.name}] {old_state.value} -> {new_state.value}")

        # 重置半开状态的调用计数
        if new_state == CircuitState.HALF_OPEN:
            self._half_open_calls = 0

    def record_success(self) -> None:
        """记录成功调用"""
        with self._lock:
            self._stats.total_calls += 1
            self._stats.successful_calls += 1
            self._stats.consecutive_failures = 0
            self._stats.consecutive_successes += 1

            if self._state == CircuitState.HALF_OPEN:
                self._half_open_calls += 1
                if self._stats.consecutive_successes >= self.config.success_threshold:
                    self._transition_to(CircuitState.CLOSED)
                    self._stats.consecutive_successes = 0

    def record_failure(self, error: str = None) -> None:
        """记录失败调用"""
        with self._lock:
            self._stats.total_calls += 1
            self._stats.failed_calls += 1
            self._stats.consecutive_failures += 1
            self._stats.consecutive_successes = 0
            self._stats.last_failure_time = time.time()
            self._stats.last_failure_error = error

            if self._state == CircuitState.HALF_OPEN:
                # 半开状态下任何失败都会重新打开
                self._transition_to(CircuitState.OPEN)
            elif self._state == CircuitState.CLOSED:
                if self._stats.consecutive_failures >= self.config.failure_threshold:
                    self._transition_to(CircuitState.OPEN)

    def can_execute(self) -> bool:
        """检查是否可以执行"""
        return self.state != CircuitState.OPEN

    def execute(self, func: Callable, *args, **kwargs) -> Any:
        """
        执行函数，带熔断保护

        Args:
            func: 要执行的函数
            *args, **kwargs: 函数参数

        Returns:
            函数返回值

        Raises:
            CircuitBreakerOpenError: 熔断器处于 OPEN 状态
        """
        if not self.can_execute():
            raise CircuitBreakerOpenError(
                f"CircuitBreaker '{self.name}' is OPEN. "
                f"Will retry after {self.config.timeout}s."
            )

        try:
            result = func(*args, **kwargs)
            self.record_success()
            return result
        except Exception as e:
            self.record_failure(str(e))
            raise

    def get_stats(self) -> dict:
        """获取统计信息"""
        with self._lock:
            return {
                "name": self.name,
                "state": self.state.value,
                "total_calls": self._stats.total_calls,
                "successful_calls": self._stats.successful_calls,
                "failed_calls": self._stats.failed_calls,
                "consecutive_failures": self._stats.consecutive_failures,
                "last_failure_time": self._stats.last_failure_time,
                "last_failure_error": self._stats.last_failure_error,
            }

    def reset(self) -> None:
        """手动重置熔断器"""
        with self._lock:
            self._state = CircuitState.CLOSED
            self._stats = CircuitStats()
            self._half_open_calls = 0
            print(f"[CircuitBreaker:{self.name}] Manual reset to CLOSED")


class CircuitBreakerOpenError(Exception):
    """熔断器打开异常"""
    pass


class CircuitBreakerDecorator:
    """装饰器方式使用 Circuit Breaker"""

    def __init__(self, name: str, config: CircuitBreakerConfig = None):
        self.breaker = CircuitBreaker(name, config)

    def __call__(self, func: Callable) -> Callable:
        def wrapper(*args, **kwargs):
            return self.breaker.execute(func, *args, **kwargs)
        return wrapper
