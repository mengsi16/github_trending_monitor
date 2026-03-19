"""GitHub API 工具 (s02)"""
import os
import httpx
from typing import List, Dict, Any

GITHUB_API_URL = "https://api.github.com"

class GitHubClient:
    def __init__(self, token: str = None):
        self.token = token or os.getenv("GITHUB_TOKEN", "")
        self.headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "GitHub-Trending-Monitor/1.0",
        }
        if self.token:
            self.headers["Authorization"] = f"token {self.token}"

    def get_trending(self, language: str = "", top_n: int = 20) -> List[Dict]:
        """获取 GitHub 热榜"""
        url = f"{GITHUB_API_URL}/search/repositories"
        params = {
            "q": f"created:>2024-01-01{' language:' + language if language else ''}",
            "sort": "stars",
            "order": "desc",
            "per_page": top_n,
        }

        with httpx.Client(timeout=30.0) as client:
            resp = client.get(url, headers=self.headers, params=params)
            resp.raise_for_status()
            data = resp.json()
            return [item for item in data.get("items", [])[:top_n]]

    def get_readme(self, owner: str, repo: str) -> str:
        """获取仓库 README"""
        url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/readme"
        with httpx.Client(timeout=30.0) as client:
            resp = client.get(url, headers=self.headers)
            if resp.status_code == 404:
                return ""
            resp.raise_for_status()
            data = resp.json()
            import base64
            content = data.get("content", "")
            if content:
                return base64.b64decode(content).decode("utf-8")
            return ""

    def get_repo_details(self, owner: str, repo: str) -> Dict:
        """获取仓库详情"""
        url = f"{GITHUB_API_URL}/repos/{owner}/{repo}"
        with httpx.Client(timeout=30.0) as client:
            resp = client.get(url, headers=self.headers)
            resp.raise_for_status()
            data = resp.json()
            return {
                "name": data.get("name"),
                "full_name": data.get("full_name"),
                "description": data.get("description", ""),
                "stars": data.get("stargazers_count", 0),
                "language": data.get("language", ""),
                "topics": data.get("topics", []),
                "url": data.get("html_url"),
            }

def tool_github_trending(language: str = "", top_n: int = 20) -> str:
    """获取 GitHub 热榜项目"""
    client = GitHubClient()
    projects = client.get_trending(language, top_n)

    if not projects:
        return "未找到热榜项目"

    result = []
    for i, p in enumerate(projects, 1):
        result.append(f"{i}. {p['full_name']} ⭐ {p.get('stargazers_count', 0)}")
        desc = p.get('description') or '无描述'
        result.append(f"   {desc[:100]}")
        result.append(f"   语言: {p.get('language') or '未知'}")
        result.append("")

    return "\n".join(result)

def tool_github_fetch_readme(owner: str, repo: str) -> str:
    """获取项目 README"""
    client = GitHubClient()
    readme = client.get_readme(owner, repo)

    if not readme:
        return f"未找到 {owner}/{repo} 的 README"

    if len(readme) > 15000:
        readme = readme[:15000] + "\n\n... [内容过长，已截断]"

    return readme

GITHUB_TOOLS = [
    {
        "name": "github_trending",
        "description": "获取 GitHub 热榜项目列表",
        "input_schema": {
            "type": "object",
            "properties": {
                "language": {"type": "string", "description": "编程语言筛选，如 python, javascript"},
                "top_n": {"type": "integer", "description": "返回数量，默认 20", "default": 20}
            }
        }
    },
    {
        "name": "github_fetch_readme",
        "description": "获取指定项目的 README 内容",
        "input_schema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "仓库所有者"},
                "repo": {"type": "string", "description": "仓库名称"}
            },
            "required": ["owner", "repo"]
        }
    }
]

GITHUB_HANDLERS = {
    "github_trending": tool_github_trending,
    "github_fetch_readme": tool_github_fetch_readme,
}
