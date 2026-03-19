"""投递队列 (s08) - WRITE-AHEAD 机制"""
import json
import os
import uuid
import time
import threading
import logging
from pathlib import Path
from dataclasses import dataclass, asdict

# 使用 __file__ 计算项目根目录的路径，避免相对路径问题
QUEUE_DIR = Path(__file__).parent.parent.parent / "workspace" / ".delivery"
_logger = logging.getLogger("delivery_queue")

@dataclass
class QueuedDelivery:
    """投递条目"""
    id: str
    channel: str      # email, feishu
    to: str          # 收件人/群ID
    subject: str     # 邮件主题
    text: str        # 内容
    enqueued_at: float
    next_retry_at: float = 0.0
    retry_count: int = 0
    last_error: str = ""

class DeliveryQueue:
    """磁盘持久化的投递队列 (s08)"""

    def __init__(self, queue_dir: Path = None):
        self.queue_dir = queue_dir or QUEUE_DIR
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _entry_path(self, delivery_id: str) -> Path:
        return self.queue_dir / f"{delivery_id}.json"

    def enqueue(self, channel: str, to: str, text: str, subject: str = "") -> str:
        """入队，线程安全"""
        with self._lock:
            delivery_id = uuid.uuid4().hex[:12]
            entry = QueuedDelivery(
                id=delivery_id,
                channel=channel,
                to=to,
                subject=subject,
                text=text,
                enqueued_at=time.time(),
            )
            self._write_entry(entry)
            return delivery_id

    def _write_entry(self, entry: QueuedDelivery):
        """原子写入 (WRITE-AHEAD)"""
        tmp_path = self.queue_dir / f".tmp.{os.getpid()}.{entry.id}.json"
        final_path = self._entry_path(entry.id)

        data = json.dumps(asdict(entry), ensure_ascii=False, indent=2)

        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())

        os.replace(str(tmp_path), str(final_path))

    def _read_entry(self, delivery_id: str) -> QueuedDelivery | None:
        """读取条目"""
        entry_path = self._entry_path(delivery_id)
        if not entry_path.exists():
            return None
        try:
            data = json.loads(entry_path.read_text(encoding="utf-8"))
            return QueuedDelivery(**data)
        except (json.JSONDecodeError, KeyError):
            return None

    def load_pending(self) -> list:
        """加载所有待投递"""
        now = time.time()
        results = []

        with self._lock:
            for path in self.queue_dir.glob("*.json"):
                if path.name.startswith(".tmp"):
                    continue
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    entry = QueuedDelivery(**data)
                    if entry.next_retry_at <= now:
                        results.append(entry)
                except Exception as e:
                    _logger.warning(f"Failed to parse queue entry {path.name}: {e}")

        return results

    def ack(self, delivery_id: str):
        """投递成功，删除"""
        with self._lock:
            path = self._entry_path(delivery_id)
            if path.exists():
                path.unlink()

    def fail(self, delivery_id: str, error: str, backoff: list, max_retries: int = 5):
        """投递失败，更新重试信息"""
        with self._lock:
            entry = self._read_entry(delivery_id)
            if entry is None:
                return

            entry.retry_count += 1
            entry.last_error = error

            if entry.retry_count >= max_retries:
                # 移到 failed 目录
                failed_dir = self.queue_dir / "failed"
                failed_dir.mkdir(exist_ok=True)
                import shutil
                src = self._entry_path(delivery_id)
                if src.exists():
                    shutil.move(str(src), str(failed_dir / f"{delivery_id}.json"))
                return

            # 计算下次重试时间
            idx = min(entry.retry_count - 1, len(backoff) - 1)
            backoff_sec = backoff[idx]
            entry.next_retry_at = time.time() + backoff_sec

            self._write_entry(entry)
