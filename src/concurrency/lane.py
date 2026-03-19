"""Lane 并发控制 (s10) - 用户优先级"""
import threading
from typing import Callable, Any
from concurrent.futures import Future

class LaneQueue:
    """Lane 队列 (s10) - 带 generation 追踪"""

    def __init__(self, name: str, max_concurrency: int = 1):
        self.name = name
        self.max_concurrency = max(1, max_concurrency)
        self._deque = []
        self._condition = threading.Condition()
        self._active_count = 0
        self._generation = 0

    def enqueue(self, fn: Callable, generation: int = None) -> Future:
        """入队，返回 Future"""
        future = Future()
        gen = generation if generation is not None else self._generation

        with self._condition:
            self._deque.append((fn, future, gen))
            self._pump()

        return future

    def _pump(self):
        """取出并启动任务"""
        while self._active_count < self.max_concurrency and self._deque:
            fn, future, gen = self._deque.pop(0)
            self._active_count += 1
            threading.Thread(
                target=self._run_task,
                args=(fn, future, gen),
                daemon=True
            ).start()

    def _run_task(self, fn: Callable, future: Future, gen: int):
        """运行任务"""
        try:
            result = fn()
            future.set_result(result)
        except Exception as exc:
            future.set_exception(exc)
        finally:
            self._task_done(gen)

    def _task_done(self, gen: int):
        with self._condition:
            self._active_count -= 1
            if gen == self._generation:
                self._pump()
            self._condition.notify_all()

    def reset(self):
        """重置 generation"""
        with self._condition:
            self._generation += 1

class LaneManager:
    """Lane 管理器"""

    def __init__(self):
        self._lanes = {}
        self._lock = threading.Lock()

    def get_or_create(self, name: str, max_concurrency: int = 1) -> LaneQueue:
        with self._lock:
            if name not in self._lanes:
                self._lanes[name] = LaneQueue(name, max_concurrency)
            return self._lanes[name]

    def reset_all(self):
        """重置所有 lane"""
        with self._lock:
            for lane in self._lanes.values():
                lane.reset()
