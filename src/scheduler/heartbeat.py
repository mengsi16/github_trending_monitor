"""心跳 (s07)"""
import time
import threading
from typing import Callable, Tuple
from datetime import datetime

class Heartbeat:
    """心跳任务 - 定时检查并执行"""

    def __init__(self, interval: int = 60, active_hours: tuple = (0, 24)):
        self.interval = interval  # 秒
        self.active_hours = active_hours  # (start_hour, end_hour)
        self.running = False
        self.last_run_at = 0.0
        self._thread = None
        self._stop_event = threading.Event()

    def should_run(self) -> Tuple[bool, str]:
        """检查是否应该运行"""
        # 检查时间间隔
        elapsed = time.time() - self.last_run_at
        if elapsed < self.interval:
            return False, f"interval not elapsed ({self.interval - elapsed:.0f}s remaining)"

        # 检查活跃时间
        hour = datetime.now().hour
        s, e = self.active_hours
        in_hours = (s <= hour < e) if s <= e else not (e <= hour < s)
        if not in_hours:
            return False, f"outside active hours ({s}:00-{e}:00)"

        if self.running:
            return False, "already running"

        return True, "all checks passed"

    def execute(self, func: Callable):
        """执行任务"""
        ok, reason = self.should_run()
        if not ok:
            return

        self.running = True
        self.last_run_at = time.time()

        try:
            func()
        finally:
            self.running = False

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
