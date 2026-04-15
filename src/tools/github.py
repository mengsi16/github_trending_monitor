"""GitHub API/Trending 工具 (s02)"""
import os
import re
import httpx
from typing import List, Dict, Any
from bs4 import BeautifulSoup

GITHUB_API_URL = "https://api.github.com"
GITHUB_TRENDING_URL = "https://github.com/trending"

class GitHubClient:
    def __init__(self, token: str = None):
        self.token = token or os.getenv("GITHUB_TOKEN", "")
        self.headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "GitHub-Trending-Monitor/1.0",
        }
        if self.token:
            self.headers["Authorization"] = f"token {self.token}"

    @staticmethod
    def _parse_int(text: str) -> int:
        """从字符串中提取整数（支持 1,234 格式）"""
        if not text:
            return 0
        match = re.search(r"([\d,]+)", text)
        if not match:
            return 0
        return int(match.group(1).replace(",", ""))

    def get_trending(self, language: str = "", top_n: int = 20, since: str = "daily") -> List[Dict]:
        """抓取 github.com/trending 热榜（daily/weekly/monthly）"""
        since = (since or "daily").lower()
        if since not in {"daily", "weekly", "monthly"}:
            since = "daily"

        lang = (language or "").strip().lower()
        url = f"{GITHUB_TRENDING_URL}/{lang}" if lang else GITHUB_TRENDING_URL
        params = {"since": since}

        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            resp = client.get(url, headers=self.headers, params=params)
            resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        rows = soup.select("article.Box-row")

        projects: List[Dict] = []
        for row in rows:
            title_a = row.select_one("h2 a")
            if not title_a:
                continue

            href = (title_a.get("href") or "").strip()
            if not href.startswith("/"):
                continue

            full_name = href.strip("/")
            if "/" not in full_name:
                continue

            owner, repo = full_name.split("/", 1)
            desc_node = row.select_one("p")
            desc = desc_node.get_text(" ", strip=True) if desc_node else ""

            lang_node = row.select_one('span[itemprop="programmingLanguage"]')
            language_name = lang_node.get_text(" ", strip=True) if lang_node else ""

            stars_total = 0
            stars_today = 0
            for anchor in row.select("a"):
                ahref = (anchor.get("href") or "").strip()
                if ahref.endswith("/stargazers") and full_name in ahref:
                    stars_total = self._parse_int(anchor.get_text(" ", strip=True))
                    break

            today_node = row.select_one("span.d-inline-block.float-sm-right")
            if today_node:
                stars_today = self._parse_int(today_node.get_text(" ", strip=True))

            projects.append({
                "repo_id": full_name,
                "owner": owner,
                "repo": repo,
                "name": repo,
                "full_name": full_name,
                "description": desc,
                "language": language_name,
                "stargazers_count": stars_total,
                "trending_stars": stars_today,
                "url": f"https://github.com/{full_name}",
                "since": since,
            })

            if len(projects) >= top_n:
                break

        return projects

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

def tool_github_trending(language: str = "", top_n: int = 20, since: str = "daily") -> str:
    """获取 GitHub 热榜项目"""
    client = GitHubClient()
    projects = client.get_trending(language=language, top_n=top_n, since=since)

    if not projects:
        return "未找到热榜项目（可能被限流或页面结构变更）"

    result = []
    for i, p in enumerate(projects, 1):
        result.append(
            f"{i}. {p['full_name']} 今日+{p.get('trending_stars', 0)} | 总⭐ {p.get('stargazers_count', 0)}"
        )
        desc = p.get('description') or '无描述'
        result.append(f"   {desc[:100]}")
        result.append(f"   语言: {p.get('language') or '未知'}")
        result.append(f"   周期: {p.get('since', since)}")
        result.append("")

    return "\n".join(result)

def tool_github_fetch_readme(owner: str, repo: str) -> str:
    """获取项目 README"""
    client = GitHubClient()
    readme = client.get_readme(owner, repo)

    if not readme:
        return f"未找到 {owner}/{repo} 的 README"

    if len(readme) > 8000:
        readme = readme[:8000] + "\n\n... [内容过长，已截断]"

    return readme

GITHUB_TOOLS = [
    {
        "name": "github_trending",
        "description": "获取 GitHub 热榜项目列表",
        "input_schema": {
            "type": "object",
            "properties": {
                "language": {"type": "string", "description": "编程语言筛选，如 python, javascript"},
                "top_n": {"type": "integer", "description": "返回数量，默认 20", "default": 20},
                "since": {
                    "type": "string",
                    "description": "热榜周期：daily/weekly/monthly，默认 daily",
                    "default": "daily"
                }
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
