"""Memory watchdog tool.

用途：
  1. 周期性采样当前 Python 进程的 RSS / VMS / 线程数 / 句柄数，
     写入 workspace/.logs/memory.log，便于事后排查 OOM。
  2. 当 RSS 超过软阈值 (MEMWD_SOFT_RSS_MB) 时启动 tracemalloc 并
     周期性生成快照到 workspace/.logs/tracemalloc/。
  3. 当 RSS 超过硬阈值 (MEMWD_HARD_RSS_MB) 时记录 critical 日志、
     写入 workspace/.restart 哨兵文件，随后以退出码 42 主动退出，
     交给 scripts/run_with_watchdog.ps1 自动重启。

设计原则：
  - 可通过 MEMWD_ENABLED=false 完全关闭（测试环境默认关）。
  - psutil 若未安装则静默跳过，不影响主流程。
  - 线程是 daemon，幂等启动，只会起一次。
  - 纯扩展 Tool，不修改任何核心模块。

作为 Claude Tool 对外暴露 memory_snapshot，可以在 QA 对话里直接查询。
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
import tracemalloc
from datetime import datetime
from pathlib import Path
from typing import Dict

try:
    import psutil  # type: ignore
    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False


_logger = logging.getLogger("memory_watchdog")

# --- 路径 ---
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_LOG_DIR = _PROJECT_ROOT / "workspace" / ".logs"
_MEMORY_LOG = _LOG_DIR / "memory.log"
_TRACE_DIR = _LOG_DIR / "tracemalloc"
_RESTART_SENTINEL = _PROJECT_ROOT / "workspace" / ".restart"

# --- 配置（环境变量覆盖） ---
_INTERVAL_SEC = max(10, int(os.getenv("MEMWD_INTERVAL_SEC", "300")))
_SOFT_RSS_MB = int(os.getenv("MEMWD_SOFT_RSS_MB", "1500"))
_HARD_RSS_MB = int(os.getenv("MEMWD_HARD_RSS_MB", "3000"))
_TRACE_TOP_N = int(os.getenv("MEMWD_TRACE_TOP_N", "30"))
_RESTART_EXIT_CODE = int(os.getenv("MEMWD_RESTART_EXIT_CODE", "42"))
# 测试环境自动禁用，避免污染 pytest 进程
_DEFAULT_ENABLED = "false" if os.getenv("PYTEST_CURRENT_TEST") else "true"
_ENABLED = os.getenv("MEMWD_ENABLED", _DEFAULT_ENABLED).lower() == "true"

# --- 状态 ---
_started = False
_lock = threading.Lock()
_tracemalloc_started = False


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _append_memory_log(line: str) -> None:
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(_MEMORY_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as e:
        _logger.warning("memory.log 写入失败: %s", e)


def _sample_once() -> Dict[str, float]:
    proc = psutil.Process(os.getpid())
    mem = proc.memory_info()
    try:
        if hasattr(proc, "num_handles"):
            handles = proc.num_handles()  # Windows
        else:
            handles = proc.num_fds()  # POSIX
    except Exception:
        handles = -1
    return {
        "pid": proc.pid,
        "rss_mb": round(mem.rss / 1024 / 1024, 1),
        "vms_mb": round(mem.vms / 1024 / 1024, 1),
        "threads": proc.num_threads(),
        "handles": handles,
    }


def _take_tracemalloc_snapshot(reason: str) -> None:
    """首次调用启动 tracemalloc，之后每次保存一个 top-N 快照。"""
    global _tracemalloc_started
    if not _tracemalloc_started:
        tracemalloc.start(25)
        _tracemalloc_started = True
        _logger.warning("[MemoryWatchdog] tracemalloc 已开启 (reason=%s)", reason)
        return

    try:
        _TRACE_DIR.mkdir(parents=True, exist_ok=True)
        snapshot = tracemalloc.take_snapshot()
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        out_path = _TRACE_DIR / f"trace-{ts}.txt"
        top_stats = snapshot.statistics("lineno")[:_TRACE_TOP_N]
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(f"# Tracemalloc snapshot {_now_iso()}\n")
            f.write(f"# Reason: {reason}\n")
            f.write(f"# Top {_TRACE_TOP_N} by lineno\n\n")
            for stat in top_stats:
                f.write(str(stat) + "\n")
        _logger.warning("[MemoryWatchdog] tracemalloc 快照: %s", out_path)
    except Exception as e:
        _logger.exception("tracemalloc 快照失败: %s", e)


def _request_restart(rss_mb: float) -> None:
    """写入哨兵文件并退出，外层 PowerShell 看门狗会重启。"""
    try:
        _RESTART_SENTINEL.parent.mkdir(parents=True, exist_ok=True)
        _RESTART_SENTINEL.write_text(
            f"hard_rss={rss_mb}MB at {_now_iso()} pid={os.getpid()}",
            encoding="utf-8",
        )
    except OSError as e:
        _logger.warning("写入 restart sentinel 失败: %s", e)

    _append_memory_log(
        f"[{_now_iso()}] HARD LIMIT EXCEEDED rss={rss_mb}MB "
        f"hard={_HARD_RSS_MB}MB -> os._exit({_RESTART_EXIT_CODE})"
    )
    # 不走 sys.exit，避免被上层异常处理吃掉；不走 atexit，直接退出让看门狗重启
    os._exit(_RESTART_EXIT_CODE)


def _watchdog_loop() -> None:
    _append_memory_log(
        f"[{_now_iso()}] watchdog started pid={os.getpid()} "
        f"interval={_INTERVAL_SEC}s soft={_SOFT_RSS_MB}MB hard={_HARD_RSS_MB}MB"
    )

    while True:
        try:
            info = _sample_once()
            line = (
                f"[{_now_iso()}] pid={info['pid']} "
                f"rss={info['rss_mb']}MB vms={info['vms_mb']}MB "
                f"threads={info['threads']} handles={info['handles']}"
            )
            _append_memory_log(line)

            rss_mb = info["rss_mb"]
            if rss_mb >= _HARD_RSS_MB:
                _logger.critical(
                    "[MemoryWatchdog] RSS %sMB 超过硬阈值 %sMB，触发重启",
                    rss_mb, _HARD_RSS_MB,
                )
                try:
                    _take_tracemalloc_snapshot(f"hard_rss={rss_mb}MB")
                except Exception:
                    pass
                _request_restart(rss_mb)
                return  # 理论不可达
            elif rss_mb >= _SOFT_RSS_MB:
                _logger.warning(
                    "[MemoryWatchdog] RSS %sMB 超过软阈值 %sMB，采快照",
                    rss_mb, _SOFT_RSS_MB,
                )
                _take_tracemalloc_snapshot(f"soft_rss={rss_mb}MB")
        except Exception as e:
            _logger.exception("[MemoryWatchdog] 采样循环异常: %s", e)

        time.sleep(_INTERVAL_SEC)


def start() -> bool:
    """启动内存监控后台线程。幂等。返回是否真正启动。"""
    global _started
    if not _ENABLED:
        _logger.info("[MemoryWatchdog] 已禁用 (MEMWD_ENABLED=false)")
        return False
    if not _HAS_PSUTIL:
        _logger.warning(
            "[MemoryWatchdog] psutil 未安装，跳过内存监控 "
            "(pip install psutil 后自动启用)"
        )
        return False

    with _lock:
        if _started:
            return True
        t = threading.Thread(
            target=_watchdog_loop,
            name="memory-watchdog",
            daemon=True,
        )
        t.start()
        _started = True

    _logger.info(
        "[MemoryWatchdog] 已启动 interval=%ss soft=%sMB hard=%sMB",
        _INTERVAL_SEC, _SOFT_RSS_MB, _HARD_RSS_MB,
    )
    print(
        f"[MemoryWatchdog] 已启动 interval={_INTERVAL_SEC}s "
        f"soft={_SOFT_RSS_MB}MB hard={_HARD_RSS_MB}MB"
    )
    return True


# --- 对外 Tool 接口：memory_snapshot ---

def memory_snapshot_handler(**kwargs) -> str:
    """查询当前进程内存、线程、句柄数量。"""
    if not _HAS_PSUTIL:
        return "psutil 未安装，无法查询进程内存"
    try:
        info = _sample_once()
        return (
            f"pid={info['pid']} RSS={info['rss_mb']}MB VMS={info['vms_mb']}MB "
            f"threads={info['threads']} handles={info['handles']} "
            f"(soft={_SOFT_RSS_MB}MB, hard={_HARD_RSS_MB}MB)"
        )
    except Exception as e:
        return f"memory_snapshot 失败: {e}"


MEMORY_WATCHDOG_TOOLS = [
    {
        "name": "memory_snapshot",
        "description": (
            "查看 monitor 进程当前的内存占用（RSS/VMS）、线程数、句柄数。"
            "用于排查内存泄漏或确认守护进程健康。"
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    }
]

MEMORY_WATCHDOG_HANDLERS = {
    "memory_snapshot": memory_snapshot_handler,
}


__all__ = [
    "MEMORY_WATCHDOG_TOOLS",
    "MEMORY_WATCHDOG_HANDLERS",
    "memory_snapshot_handler",
    "start",
]
