"""心跳 (s07) - Lane 互斥实现用户输入优先"""
import time
import threading
from typing import Callable, Tuple, Any, List
from datetime import datetime


class OutputQueue:
    """线程安全的输出队列 (s07)"""

    def __init__(self):
        self._queue: List[str] = []
        self._lock = threading.Lock()

    def append(self, item: str):
        with self._lock:
            self._queue.append(item)

    def drain(self) -> List[str]:
        with self._lock:
            items = self._queue.copy()
            self._queue.clear()
            return items

    def __len__(self) -> int:
        with self._lock:
            return len(self._queue)


class Heartbeat:
    """心跳任务 - 定时检查并执行 (s07)

    核心设计: Lane 互斥
    - 用户消息: blocking=True (始终优先)
    - Heartbeat: blocking=False (用户活跃时让步)

    使用约定:
    - 返回 "HEARTBEAT_OK" 表示无内容需要报告，抑制输出
    - 其他返回值会通过 output_queue 投递到 REPL
    """

    HEARTBEAT_OK = "HEARTBEAT_OK"

    def __init__(self, interval: int = 60, active_hours: tuple = (0, 24),
                 lane_lock: threading.Lock = None):
        self.interval = interval  # 秒
        self.active_hours = active_hours  # (start_hour, end_hour)
        self.lane_lock = lane_lock or threading.Lock()  # Lane 互斥锁
        self.running = False
        self.last_run_at = 0.0
        self._thread = None
        self._stop_event = threading.Event()
        self._output_queue = OutputQueue()
        self._last_output = ""

    def should_run(self) -> Tuple[bool, str]:
        """检查是否应该运行 (4 个前置条件)"""
        # 1. 检查时间间隔
        elapsed = time.time() - self.last_run_at
        if elapsed < self.interval:
            return False, f"interval not elapsed ({self.interval - elapsed:.0f}s remaining)"

        # 2. 检查活跃时间
        hour = datetime.now().hour
        s, e = self.active_hours
        in_hours = (s <= hour < e) if s <= e else not (e <= hour < s)
        if not in_hours:
            return False, f"outside active hours ({s}:00-{e}:00)"

        # 3. 检查是否已在运行
        if self.running:
            return False, "already running"

        return True, "all checks passed"

    def _try_acquire_lane(self) -> bool:
        """尝试获取 Lane (非阻塞)"""
        return self.lane_lock.acquire(blocking=False)

    def execute(self, func: Callable) -> Any:
        """执行任务 - Lane 互斥版本

        Returns:
            任务的返回值，或 None (Lane 被占用)
        """
        ok, reason = self.should_run()
        if not ok:
            return None

        # 尝试非阻塞获取 Lane
        acquired = self._try_acquire_lane()
        if not acquired:
            # 用户持有锁，跳过本次心跳
            return None

        self.running = True
        self.last_run_at = time.time()

        try:
            result = func()

            # 检查是否是 HEARTBEAT_OK
            if result is None:
                return None

            result_str = str(result).strip() if result else ""

            # 如果是 HEARTBEAT_OK 且和上次相同，抑制输出
            if result_str == self.HEARTBEAT_OK:
                return None

            # 有意义的输出，检查是否和上次重复
            if result_str and result_str != self._last_output:
                self._last_output = result_str
                self._output_queue.append(result_str)

            return result
        finally:
            self.running = False
            self.lane_lock.release()

    def start(self, func: Callable):
        """启动心跳"""
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, args=(func,), daemon=True)
        self._thread.start()

    def stop(self):
        """停止心跳"""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self, func: Callable):
        """心跳循环"""
        while not self._stop_event.is_set():
            self.execute(func)
            self._stop_event.wait(self.interval)

    def get_pending_output(self) -> List[str]:
        """获取待处理的输出"""
        return self._output_queue.drain()

    def drain_output(self) -> List[str]:
        """获取并清空所有待处理输出"""
        return self._output_queue.drain()

    @property
    def output_queue_size(self) -> int:
        """获取输出队列大小"""
        return len(self._output_queue)
