"""爬虫 Agent (s02)"""
import json
import logging
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from .base import BaseAgent
from ..tools.github import GitHubClient
from ..tools.chromadb import RAGStore

_logger = logging.getLogger("crawler")

# brain-base-cli 路径（仅依赖 BRAIN_BASE_PATH 环境变量；未设置则跳过双写）
_BRAIN_BASE_PATH_ENV = os.environ.get("BRAIN_BASE_PATH", "").strip()
_BRAIN_BASE_CLI = (
    Path(_BRAIN_BASE_PATH_ENV) / "bin" / "brain-base-cli.py"
    if _BRAIN_BASE_PATH_ENV
    else None
)


def _brain_base_ingest_url(url: str, topic: str = "") -> dict:
    """调用 brain-base-cli ingest-url，返回 JSON 结果。失败不抛异常。"""
    if _BRAIN_BASE_CLI is None:
        return {"ok": False, "error": "BRAIN_BASE_PATH is not set"}
    if not _BRAIN_BASE_CLI.exists():
        return {"ok": False, "error": f"brain-base-cli not found: {_BRAIN_BASE_CLI}"}
    argv = [
        sys.executable, str(_BRAIN_BASE_CLI),
        "ingest-url", "--url", url,
    ]
    if topic:
        argv.extend(["--topic", topic])
    try:
        proc = subprocess.run(
            argv,
            capture_output=True, text=True,
            timeout=3600,
            encoding="utf-8", errors="replace",
        )
        try:
            payload = json.loads(proc.stdout) if proc.stdout.strip() else {}
        except json.JSONDecodeError:
            payload = {"raw_stdout": proc.stdout[:500]}
        payload["exit_code"] = proc.returncode
        payload["ok"] = proc.returncode == 0
        if proc.stderr:
            payload["stderr_preview"] = proc.stderr[:300]
        return payload
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"brain-base ingest-url timeout (3600s): {url}"}
    except Exception as e:
        return {"ok": False, "error": f"brain-base ingest-url failed: {e}"}

def _fire_and_forget_ingest(tasks: list):
    """后台并发执行 brain-base ingest-url，fire-and-forget，不阻塞调用方。

    Args:
        tasks: [(full_name, url, topic), ...]
    """
    import threading

    if _BRAIN_BASE_CLI is None:
        _logger.info(
            "brain-base: BRAIN_BASE_PATH not set, skipping double-write for %d projects",
            len(tasks),
        )
        return

    def _worker():
        bb_ok = 0
        bb_fail = 0
        _logger.info("brain-base: starting %d concurrent ingest-url tasks", len(tasks))
        with ThreadPoolExecutor(max_workers=min(len(tasks), 8)) as pool:
            futures = {
                pool.submit(_brain_base_ingest_url, url, topic): name
                for name, url, topic in tasks
            }
            for fut in as_completed(futures):
                name = futures[fut]
                try:
                    bb_result = fut.result()
                except Exception as exc:
                    bb_result = {"ok": False, "error": str(exc)}
                if bb_result.get("ok"):
                    bb_ok += 1
                    _logger.info("brain-base ingest OK: %s", name)
                else:
                    bb_fail += 1
                    _logger.warning("brain-base ingest FAIL: %s – %s", name, bb_result.get("error", "unknown"))
        _logger.info("brain-base: done (ok=%d, fail=%d)", bb_ok, bb_fail)

    t = threading.Thread(target=_worker, daemon=True, name="bb-ingest")
    t.start()


CRAWLER_PROMPT = """你是一个 GitHub 热榜爬虫 Agent。

你的任务：
1. 抓取 github.com/trending 真实热榜（默认 daily）
2. 爬取每个项目的 README
3. 每个项目同时写入 ChromaDB 和 brain-base（双写）

请按顺序执行，输出每一步的结果。"""

class CrawlerAgent(BaseAgent):
    """爬虫 Agent - 定时爬取 GitHub 热榜"""

    def _default_prompt(self) -> str:
        return CRAWLER_PROMPT

    def run_crawl(self, top_n: int = None) -> str:
        """执行真实 Trending 爬取，并重建 Chroma 数据。"""
        from src.config import config

        top_n = top_n if top_n is not None else config.github_top_n

        github = GitHubClient()
        store = RAGStore()

        projects = github.get_trending(top_n=top_n, since="daily")
        if not projects:
            return "爬取失败：未获取到 GitHub Trending 数据"

        now = datetime.now()
        crawl_date = now.strftime("%Y-%m-%d")
        crawl_ts = now.isoformat(timespec="seconds")
        crawl_batch_id = now.isoformat(timespec="microseconds")

        stored = 0
        status_counts = {"new": 0, "updated": 0, "unchanged": 0}
        errors = []
        write_errors = []
        bb_tasks = []  # (full_name, url, topic)
        for rank, project in enumerate(projects, 1):
            owner = project.get("owner", "")
            repo = project.get("repo", "")
            full_name = project.get("full_name") or f"{owner}/{repo}"

            if not owner or not repo:
                errors.append(f"{rank}. 项目名称解析失败: {full_name}")
                continue

            try:
                details = github.get_repo_details(owner, repo)
            except Exception:
                details = {
                    "name": repo,
                    "full_name": full_name,
                    "description": project.get("description", ""),
                    "stars": project.get("stargazers_count", 0),
                    "language": project.get("language", ""),
                    "topics": [],
                    "url": project.get("url", f"https://github.com/{full_name}"),
                }

            try:
                readme = github.get_readme(owner, repo)
            except Exception:
                readme = ""

            try:
                write_result = store.add_project({
                    "repo_id": full_name,
                    "repo_name": full_name,
                    "description": details.get("description") or project.get("description", ""),
                    "language": details.get("language") or project.get("language", ""),
                    "stars": details.get("stars", project.get("stargazers_count", 0)),
                    "today_stars": project.get("trending_stars", 0),
                    "since": project.get("since", "daily"),
                    "rank": rank,
                    "topics": details.get("topics", []),
                    "url": details.get("url") or project.get("url", f"https://github.com/{full_name}"),
                    "crawl_date": crawl_date,
                    "crawl_ts": crawl_ts,
                    "crawl_batch_id": crawl_batch_id,
                    "readme": readme,
                })
                stored += 1
                status = write_result.get("status", "updated")
                if status in status_counts:
                    status_counts[status] += 1
            except Exception as e:
                write_errors.append(f"{full_name}: {e}")

            # ── 收集 brain-base 双写任务 ──
            project_url = details.get("url") or project.get("url", f"https://github.com/{full_name}")
            topic = f"GitHub trending project: {full_name} - {(details.get('description') or '')[:80]}"
            bb_tasks.append((full_name, project_url, topic))

        # ── 后台异步执行 brain-base 双写（不阻塞主流程） ──
        if bb_tasks:
            _fire_and_forget_ingest(bb_tasks)

        if _BRAIN_BASE_CLI is None:
            bb_status = "brain-base 双写: 已跳过（BRAIN_BASE_PATH 未设置）"
        else:
            bb_status = f"brain-base 双写: {len(bb_tasks)} 个项目已提交后台并发入库（max_workers=8，结果见日志）。"

        lines = [
            f"已完成 GitHub Trending 爬取（daily）。",
            f"请求数量: {top_n}，抓取到: {len(projects)}，ChromaDB 入库: {stored}。",
            f"去重结果: 新增 {status_counts['new']}，内容更新 {status_counts['updated']}，内容未变 {status_counts['unchanged']}。",
            f"ChromaDB 已保留历史记录，当前记录数: {store.count()}。",
            bb_status,
            "",
            "Top 项目预览:",
        ]

        for idx, item in enumerate(projects[: min(5, len(projects))], 1):
            lines.append(
                f"{idx}. {item.get('full_name')} 今日+{item.get('trending_stars', 0)} | 总⭐ {item.get('stargazers_count', 0)}"
            )

        if errors:
            lines.append("")
            lines.append(f"解析异常: {len(errors)} 条（示例: {errors[0]}）")

        if write_errors:
            lines.append("")
            lines.append(f"写入异常: {len(write_errors)} 条（示例: {write_errors[0]}）")

        return "\n".join(lines)
