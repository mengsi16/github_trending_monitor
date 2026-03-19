"""投递 Runner (s08)"""
import time
import threading
from .queue import DeliveryQueue
from src.config import config

class DeliveryRunner:
    """后台投递 runner (s08)"""

    def __init__(self):
        self.queue = DeliveryQueue()
        self.running = False
        self._thread = None
        self._stop_event = threading.Event()
        self.senders = {}  # channel -> send_func

    def register_sender(self, channel: str, send_func):
        """注册发送函数"""
        self.senders[channel] = send_func

    def start(self):
        """启动投递"""
        self.running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        """停止投递"""
        self.running = False
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self):
        """投递循环"""
        backoff = config.retry_backoff
        max_retries = config.max_retries

        while not self._stop_event.is_set():
            # 扫描待投递
            pending = self.queue.load_pending()

            for entry in pending:
                sender = self.senders.get(entry.channel)
                if not sender:
                    self.queue.fail(entry.id, f"No sender for {entry.channel}", backoff, max_retries)
                    continue

                try:
                    # 调用发送函数
                    if entry.channel == "email":
                        ok = sender(entry.to, entry.text, subject=entry.subject)
                    else:
                        ok = sender(entry.to, entry.text)

                    if ok:
                        self.queue.ack(entry.id)
                    else:
                        self.queue.fail(entry.id, "Send failed", backoff, max_retries)
                except Exception as e:
                    self.queue.fail(entry.id, str(e), backoff, max_retries)

            # 每秒检查一次
            self._stop_event.wait(1.0)

    def enqueue(self, channel: str, to: str, text: str, subject: str = ""):
        """入队"""
        return self.queue.enqueue(channel, to, text, subject)
