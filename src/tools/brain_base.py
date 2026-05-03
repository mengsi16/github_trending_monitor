"""brain-base 硬工具 —— 直接封装 brain-base-cli search/ask/exists/ingest-url/health。

优先级高于 ChromaDB RAG：QA Agent 应先调 brain_base_search/ask，
失败或无结果时降级到 rag_search。

健康检查缓存：
- brain_base_health 会实测一次 CLI，并缓存结果 60 秒
- 缓存内若标记为不可用，其他 brain_base_* 工具会 fast-fail，避免重复浪费子进程
"""
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_logger = logging.getLogger("brain_base_tools")

# ── CLI 路径（模块 import 时快照；改 .env 需重启）──
_BRAIN_BASE_PATH = os.environ.get("BRAIN_BASE_PATH", "").strip()
_BRAIN_BASE_CLI = Path(_BRAIN_BASE_PATH) / "bin" / "brain-base-cli.py" if _BRAIN_BASE_PATH else None


# ── 健康检查缓存 ──
@dataclass
class _HealthState:
    ok: bool = True            # 乐观默认（未探测前不阻止调用）
    reason: str = ""
    checked_at: float = 0.0    # 0.0 = 从未探测过
    detail: str = ""


# TTL 5 分钟：brain-base-cli health 冷启动加载 bge-m3 模型需 30~60s，
# 不必每次调用都重新探测。
_HEALTH_TTL_SECONDS = 300.0
_health: _HealthState = _HealthState()


def _cache_is_fresh() -> bool:
    """缓存未过期且已经探测过至少一次。"""
    return _health.checked_at > 0 and (time.time() - _health.checked_at) < _HEALTH_TTL_SECONDS


def _update_health(ok: bool, reason: str = "", detail: str = "") -> None:
    _health.ok = ok
    _health.reason = reason
    _health.detail = detail
    _health.checked_at = time.time()


def _summarize_health_payload(payload: dict) -> str:
    """从 brain-base-cli health 返回的 JSON 提取关键状态行。"""
    parts = []
    claude = payload.get("claude", {}) or {}
    if claude:
        parts.append(f"claude={claude.get('available', '?')} ({claude.get('version', 'n/a')})")
    milvus = payload.get("milvus", {}) or {}
    if milvus:
        parts.append(
            f"milvus={milvus.get('can_vectorize', '?')} "
            f"provider={milvus.get('provider', 'n/a')} "
            f"uri={milvus.get('milvus_uri', 'n/a')}"
        )
    dc = payload.get("doc_converter", {}) or {}
    if dc:
        mineru = (dc.get("mineru") or {}).get("available", "?")
        pandoc = (dc.get("pandoc") or {}).get("available", "?")
        parts.append(f"doc_converter: mineru={mineru}, pandoc={pandoc}")
    return " | ".join(parts) if parts else "(health payload empty)"


def _check_health_live(timeout: int = 90) -> dict:
    """实测 brain-base 可用性，并更新缓存。

    检查顺序：
    1. BRAIN_BASE_PATH 环境变量是否设置
    2. brain-base-cli.py 文件是否存在
    3. 调用 `brain-base-cli.py health` 子命令并判断 exit code

    注：first call 需加载 bge-m3 模型，实测在冷启动下约 30~60s，故 timeout 默认 90s。
    """
    if _BRAIN_BASE_CLI is None:
        _update_health(False, "BRAIN_BASE_PATH is not set")
        return {"ok": False, "error": _health.reason}
    if not _BRAIN_BASE_CLI.exists():
        _update_health(False, f"brain-base-cli not found: {_BRAIN_BASE_CLI}")
        return {"ok": False, "error": _health.reason}

    try:
        proc = subprocess.run(
            [sys.executable, str(_BRAIN_BASE_CLI), "health"],
            capture_output=True, text=True,
            timeout=timeout,
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        _update_health(False, f"health check timeout ({timeout}s)")
        return {"ok": False, "error": _health.reason}
    except Exception as e:
        _update_health(False, f"health check failed: {e}")
        return {"ok": False, "error": _health.reason}

    if proc.returncode == 0:
        # 尝试解析 JSON 提取关键信息；解析失败降级为 stdout 裁剪
        detail = ""
        try:
            payload = json.loads(proc.stdout.strip()) if proc.stdout.strip() else {}
            detail = _summarize_health_payload(payload) if payload else proc.stdout.strip()[:500]
        except json.JSONDecodeError:
            detail = proc.stdout.strip()[:500]
        _update_health(True, "", detail=detail)
        return {"ok": True, "detail": detail}

    reason = (proc.stderr.strip() or proc.stdout.strip() or f"exit_code={proc.returncode}")[:300]
    _update_health(False, reason)
    return {"ok": False, "error": reason}


def _run_cli(*args: str, timeout: int = 120, skip_health_cache: bool = False) -> dict:
    """调用 brain-base-cli，返回解析后的 JSON dict。失败不抛异常。

    若健康缓存未过期且标记为不可用，直接 fast-fail，不再起子进程。
    """
    # fast-fail：缓存内已知不可用
    if not skip_health_cache and _cache_is_fresh() and not _health.ok:
        return {
            "ok": False,
            "error": f"brain-base unhealthy (cached {int(time.time() - _health.checked_at)}s ago): {_health.reason}",
        }

    if _BRAIN_BASE_CLI is None:
        _update_health(False, "BRAIN_BASE_PATH is not set")
        return {"ok": False, "error": "BRAIN_BASE_PATH is not set"}
    if not _BRAIN_BASE_CLI.exists():
        _update_health(False, f"brain-base-cli not found: {_BRAIN_BASE_CLI}")
        return {"ok": False, "error": f"brain-base-cli not found: {_BRAIN_BASE_CLI}"}
    argv = [sys.executable, str(_BRAIN_BASE_CLI)] + list(args)
    try:
        proc = subprocess.run(
            argv,
            capture_output=True, text=True,
            timeout=timeout,
            encoding="utf-8", errors="replace",
        )
        try:
            payload = json.loads(proc.stdout) if proc.stdout.strip() else {}
        except json.JSONDecodeError:
            payload = {"raw_stdout": proc.stdout[:2000]}
        payload["exit_code"] = proc.returncode
        payload["ok"] = proc.returncode == 0
        if proc.stderr:
            payload["stderr_preview"] = proc.stderr[:500]
        return payload
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"brain-base-cli timeout ({timeout}s)"}
    except Exception as e:
        return {"ok": False, "error": f"brain-base-cli failed: {e}"}


# ── 工具函数 ──

def tool_brain_base_search(query: str, top_k: int = 5) -> str:
    """搜索 brain-base 知识库，返回匹配的文档片段。"""
    result = _run_cli("search", "--query", query, "--top-k", str(top_k))
    if not result.get("ok"):
        return f"[brain-base search 失败] {result.get('error', 'unknown')}"
    # CLI 返回的 results 字段
    results = result.get("results", [])
    if not results:
        return "[brain-base search] 无匹配结果，请降级使用 rag_search"
    parts = []
    for i, item in enumerate(results, 1):
        score = item.get("score", item.get("distance", "?"))
        text = item.get("text", item.get("content", ""))
        source = item.get("source", item.get("doc_id", ""))
        parts.append(f"--- 结果 {i} (score={score}, source={source}) ---\n{text}")
    return "\n\n".join(parts)


def tool_brain_base_ask(question: str) -> str:
    """向 brain-base 知识库提问，返回 Agentic RAG 回答（含检索+推理）。"""
    result = _run_cli("ask", "--question", question, timeout=600)
    if not result.get("ok"):
        return f"[brain-base ask 失败] {result.get('error', 'unknown')}"
    answer = result.get("answer", result.get("response", ""))
    if not answer:
        # 可能 raw_stdout 里有内容
        answer = result.get("raw_stdout", "[brain-base ask] 无回答")
    return answer


def tool_brain_base_exists(url: str) -> str:
    """检查 URL 是否已存在于 brain-base 知识库。"""
    result = _run_cli("exists", "--url", url)
    if not result.get("ok"):
        return f"[brain-base exists 失败] {result.get('error', 'unknown')}"
    exists = result.get("exists", False)
    doc_id = result.get("doc_id", "")
    return f"exists={exists}, doc_id={doc_id}"


def tool_brain_base_ingest_url(url: str, topic: str = "") -> str:
    """将 URL 内容入库到 brain-base 知识库（后台异步，不阻塞）。"""
    args = ["ingest-url", "--url", url]
    if topic:
        args.extend(["--topic", topic])
    result = _run_cli(*args, timeout=3600)
    if not result.get("ok"):
        return f"[brain-base ingest-url 失败] {result.get('error', 'unknown')}"
    return f"[brain-base ingest-url 成功] url={url}"


def tool_brain_base_enrich_chunks(doc_id: str) -> str:
    """检测并补填指定文档 chunk 的 title/summary/keywords/questions，自动重新入库。"""
    result = _run_cli("enrich-chunks", "--doc-id", doc_id, timeout=600)
    if not result.get("ok"):
        return f"[brain-base enrich-chunks 失败] {result.get('error', 'unknown')}"
    return f"[brain-base enrich-chunks 成功] doc_id={doc_id}"


def tool_brain_base_health(force: bool = False) -> str:
    """检查 brain-base 可用性。

    Args:
        force: 是否跳过 60 秒缓存强制实时探测。
    """
    if force or not _cache_is_fresh():
        result = _check_health_live()
    else:
        result = (
            {"ok": True, "detail": _health.detail or "OK"}
            if _health.ok
            else {"ok": False, "error": _health.reason}
        )

    age = int(time.time() - _health.checked_at) if _health.checked_at else -1
    tag = "cached" if (not force and _cache_is_fresh()) else "live"

    if result.get("ok"):
        path_info = str(_BRAIN_BASE_CLI) if _BRAIN_BASE_CLI else "(BRAIN_BASE_PATH unset)"
        return (
            f"[brain-base 可用 {tag}, age={age}s]\n"
            f"cli: {path_info}\n"
            f"detail: {result.get('detail', 'OK')}"
        )
    return (
        f"[brain-base 不可用 {tag}, age={age}s]\n"
        f"reason: {result.get('error', 'unknown')}\n"
        f"建议降级：直接调用 rag_search / rag_get_latest，或用 browser_navigate 补充"
    )


# ── Tool Schema（Claude function calling 格式） ──

BRAIN_BASE_TOOLS = [
    {
        "name": "brain_base_search",
        "description": "搜索 brain-base 知识库（Milvus 向量检索）。优先于 rag_search 使用。返回匹配的文档片段和来源。",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索查询，可以是项目名、技术关键词、问题描述等",
                },
                "top_k": {
                    "type": "integer",
                    "description": "返回结果数量，默认5",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "brain_base_ask",
        "description": "向 brain-base 知识库提问（Agentic RAG），返回完整回答。适合需要综合推理的复杂问题。优先于 rag_search 使用。",
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "问题，如'TradingAgent 是什么项目？'、'最近有哪些 AI agent 框架？'",
                },
            },
            "required": ["question"],
        },
    },
    {
        "name": "brain_base_exists",
        "description": "检查 URL 是否已存在于 brain-base 知识库。用于去重判断。",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "要检查的 URL",
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "brain_base_ingest_url",
        "description": "将 URL 内容入库到 brain-base 知识库。用于补库（如发现新项目未入库）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "要入库的 URL（如 GitHub 项目页）",
                },
                "topic": {
                    "type": "string",
                    "description": "入库主题描述（可选）",
                    "default": "",
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "brain_base_enrich_chunks",
        "description": "检测并补填指定文档 chunk 的 title/summary/keywords/questions enrichment 字段，自动删除 Milvus 旧行并重新入库。适用于入库后 chunk 缺少 enrichment 或格式错误需修复的场景。",
        "input_schema": {
            "type": "object",
            "properties": {
                "doc_id": {
                    "type": "string",
                    "description": "文档 ID（如 owner-repo-2026-04-29）",
                },
            },
            "required": ["doc_id"],
        },
    },
    {
        "name": "brain_base_health",
        "description": (
            "检查 brain-base 知识库是否可用：BRAIN_BASE_PATH 是否设置、brain-base-cli.py 是否存在、health 命令是否返回成功。"
            "首次调用需加载 bge-m3 模型约 30~60s；结果自动缓存 5 分钟。传 force=true 强制实时探测。"
            "建议每个会话的首轮问答调用一次，不可用时直接降级到 rag_search。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "force": {
                    "type": "boolean",
                    "description": "是否跳过缓存强制实时探测",
                    "default": False,
                },
            },
            "required": [],
        },
    },
]

BRAIN_BASE_HANDLERS = {
    "brain_base_search": tool_brain_base_search,
    "brain_base_ask": tool_brain_base_ask,
    "brain_base_exists": tool_brain_base_exists,
    "brain_base_ingest_url": tool_brain_base_ingest_url,
    "brain_base_enrich_chunks": tool_brain_base_enrich_chunks,
    "brain_base_health": tool_brain_base_health,
}
