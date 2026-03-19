"""爬虫 Agent (s02)"""
from .base import BaseAgent
from ..tools import ALL_TOOLS, ALL_HANDLERS

CRAWLER_PROMPT = """你是一个 GitHub 热榜爬虫 Agent。

你的任务：
1. 调用 github_trending 获取今日热榜
2. 对每个项目，调用 github_fetch_readme 获取 README
3. 调用 rag_store 将项目信息存入 RAG

请按顺序执行，输出每一步的结果。"""

class CrawlerAgent(BaseAgent):
    """爬虫 Agent - 定时爬取 GitHub 热榜"""

    def _default_prompt(self) -> str:
        return CRAWLER_PROMPT

    def run_crawl(self) -> str:
        """执行爬取任务"""
        messages = [{"role": "user", "content": "请开始爬取今日 GitHub 热榜 Top 20"}]
        result, _ = self.run(
            user_message="",
            messages=messages,
            tools=ALL_TOOLS,
            tool_handlers=ALL_HANDLERS
        )
        return result
