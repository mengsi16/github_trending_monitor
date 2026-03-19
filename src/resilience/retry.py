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

# 不应该重试的错误类型，直接抛出
NON_RETRYABLE_REASONS = {FailoverReason.auth, FailoverReason.billing}

def retry_with_backoff(
    func: Callable,
    max_retries: int = 3,
    backoff_ms: list = None,
    retry_on: list = None,
) -> Any:
    """
    带退避的重试 (s09)

    策略:
    - auth/billing 错误: 不重试，直接抛出（需要人工介入或切换 API Key）
    - rate_limit/timeout 错误: 使用较长延迟重试
    - 其他错误: 使用指数退避重试
    """
    backoff_ms = backoff_ms or [5000, 25000, 120000]
    last_exc = None

    for attempt in range(max_retries + 1):
        try:
            return func()
        except Exception as exc:
            last_exc = exc

            # 检查是否在 retry_on 列表中，不在则直接抛出
            if retry_on and not isinstance(exc, tuple(retry_on)):
                raise

            reason = classify_failure(exc)

            # auth/billing 错误不重试，直接抛出（可能是 API Key 失效等）
            if reason in NON_RETRYABLE_REASONS:
                raise

            # rate_limit: 较长延迟后重试
            if reason == FailoverReason.rate_limit:
                if attempt < max_retries:
                    time.sleep(120)
                    continue
                raise

            # timeout: 较短延迟后重试
            if reason == FailoverReason.timeout:
                if attempt < max_retries:
                    time.sleep(60)
                    continue
                raise

            # 其他错误: 指数退避
            if attempt < max_retries:
                idx = min(attempt, len(backoff_ms) - 1)
                delay = backoff_ms[idx]
                jitter = random.randint(-delay // 5, delay // 5)
                time.sleep(max(0, delay + jitter) / 1000)

    raise last_exc
