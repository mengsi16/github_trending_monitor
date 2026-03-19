"""熔断机制 (s09, s12.4)"""
import time
from threading import Lock
from typing import Callable, Any
from dataclasses import dataclass

@dataclass
class CircuitState:
    failure_count: int = 0
    last_failure_time: float = 0.0
    is_open: bool = False

class CircuitBreaker:
    """
    熔断器 (s12.4)
    连续 5 次失败后，熔断 10 分钟
    """

    def __init__(self, failure_threshold: int = 5, recovery_timeout: float = 600.0):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.state = CircuitState()
        self._lock = Lock()

    def call(self, func: Callable, *args, **kwargs) -> Any:
        """执行函数，失败时熔断"""
        with self._lock:
            if self.state.is_open:
                if time.time() - self.state.last_failure_time > self.recovery_timeout:
                    self.state.is_open = False
                    self.state.failure_count = 0
                else:
                    raise RuntimeError("Circuit breaker is open")

        try:
            result = func(*args, **kwargs)
            with self._lock:
                self.state.failure_count = 0
            return result

        except Exception as exc:
            with self._lock:
                self.state.failure_count += 1
                self.state.last_failure_time = time.time()

                if self.state.failure_count >= self.failure_threshold:
                    self.state.is_open = True

            raise

    def reset(self):
        """手动重置"""
        with self._lock:
            self.state = CircuitState()
