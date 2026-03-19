"""重试逻辑 (s09)"""
import time
import random
from typing import Callable, Any
from enum import Enum

class FailoverReason(Enum):
    """失败分类 (s09)"""
    rate_limit = "rate_limit"
    auth = "auth"
    timeout = "timeout"
    billing = "billing"
    overflow = "overflow"
    unknown = "unknown"

def classify_failure(exc: Exception) -> FailoverReason:
    """分类失败原因"""
    msg = str(exc).lower()

    if "rate" in msg or "429" in msg:
        return FailoverReason.rate_limit
    if "auth" in msg or "401" in msg or "key" in msg:
        return FailoverReason.auth
    if "timeout" in msg or "timed out" in msg:
        return FailoverReason.timeout
    if "billing" in msg or "quota" in msg or "402" in msg:
        return FailoverReason.billing
    if "context" in msg or "token" in msg or "overflow" in msg:
        return FailoverReason.overflow

    return FailoverReason.unknown

def retry_with_backoff(
    func: Callable,
    max_retries: int = 3,
    backoff_ms: list = None,
    retry_on: list = None,
) -> Any:
    """
    带退避的重试 (s09)
    """
    backoff_ms = backoff_ms or [5000, 25000, 120000]
    last_exc = None

    for attempt in range(max_retries + 1):
        try:
            return func()
        except Exception as exc:
            last_exc = exc

            if retry_on and not isinstance(exc, tuple(retry_on)):
                raise

            reason = classify_failure(exc)

            if reason in (FailoverReason.auth, FailoverReason.billing):
                time.sleep(300)
                continue

            if reason == FailoverReason.rate_limit:
                time.sleep(120)
                continue

            if reason == FailoverReason.timeout:
                time.sleep(60)
                continue

            if attempt < max_retries:
                idx = min(attempt, len(backoff_ms) - 1)
                delay = backoff_ms[idx]
                jitter = random.randint(-delay // 5, delay // 5)
                time.sleep(max(0, delay + jitter) / 1000)

    raise last_exc
