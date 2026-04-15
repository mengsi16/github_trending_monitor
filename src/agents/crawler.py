"""爬虫 Agent (s02)"""
from datetime import datetime
from .base import BaseAgent
from ..tools.github import GitHubClient
from ..tools.chromadb import RAGStore

CRAWLER_PROMPT = """你是一个 GitHub 热榜爬虫 Agent。

你的任务：
1. 抓取 github.com/trending 真实热榜（默认 daily）
2. 爬取每个项目的 README
3. 每次爬取前删除并重建 Chroma 集合，再写入最新榜单

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

        lines = [
            f"已完成 GitHub Trending 爬取（daily）。",
            f"请求数量: {top_n}，抓取到: {len(projects)}，入库: {stored}。",
            f"去重结果: 新增 {status_counts['new']}，内容更新 {status_counts['updated']}，内容未变 {status_counts['unchanged']}。",
            f"Chroma 已保留历史记录，当前记录数: {store.count()}。",
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
