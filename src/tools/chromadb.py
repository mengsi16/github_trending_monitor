"""ChromaDB RAG 工具 (s06)"""
import chromadb
from chromadb.config import Settings
from typing import List, Dict, Any
import uuid
from datetime import datetime

class RAGStore:
    def __init__(self, persist_dir: str = None):
        from src.config import config
        self.persist_dir = persist_dir or config.chromadb_dir
        self.client = chromadb.PersistentClient(path=self.persist_dir)
        self.collection_name = "projects"
        self.collection = self.client.get_or_create_collection(
            name=self.collection_name,
            metadata={"description": "GitHub trending projects"}
        )

    def reset_collection(self) -> None:
        """删除并重建项目集合。"""
        try:
            self.client.delete_collection(self.collection_name)
        except Exception as e:
            # 仅在 collection 不存在时忽略，其他错误应向上抛出
            err = str(e).lower()
            not_found_markers = ["does not exist", "not found", "unknown collection"]
            if not any(marker in err for marker in not_found_markers):
                raise

        self.collection = self.client.get_or_create_collection(
            name=self.collection_name,
            metadata={"description": "GitHub trending projects"}
        )

        if self.collection.count() != 0:
            raise RuntimeError("重建 Chroma 集合失败：新集合不是空状态")

    def count(self) -> int:
        """当前集合文档数量。"""
        return self.collection.count()

    def add_project(self, project_data: Dict) -> str:
        """添加项目记录（保留历史）"""
        doc_id = str(uuid.uuid4())

        doc_content = f"""
        项目: {project_data.get('repo_name')}
        描述: {project_data.get('description', '')}
        语言: {project_data.get('language', '')}
        总 Stars: {project_data.get('stars', 0)}
        趋势新增 Stars: {project_data.get('today_stars', 0)}
        热榜周期: {project_data.get('since', 'daily')}
        榜单名次: {project_data.get('rank', 0)}
        主题: {', '.join(project_data.get('topics', []))}
        README: {project_data.get('readme', '')[:5000]}
        """.strip()

        self.collection.add(
            documents=[doc_content],
            ids=[doc_id],
            metadatas=[{
                "repo_id": project_data.get("repo_id"),
                "repo_name": project_data.get("repo_name"),
                "crawl_date": project_data.get("crawl_date"),
                "crawl_ts": project_data.get("crawl_ts"),
                "stars": project_data.get("stars", 0),
                "today_stars": project_data.get("today_stars", 0),
                "since": project_data.get("since", "daily"),
                "rank": project_data.get("rank", 0),
                "url": project_data.get("url", ""),
            }]
        )

        return doc_id

    def search(self, query: str, n_results: int = 10,
               where: Dict = None) -> List[Dict]:
        """语义搜索项目"""
        results = self.collection.query(
            query_texts=[query],
            n_results=n_results,
            where=where
        )

        output = []
        if results["documents"] and results["documents"][0]:
            for i, doc in enumerate(results["documents"][0]):
                meta = results["metadatas"][0][i] if results["metadatas"] else {}
                output.append({
                    "repo_id": meta.get("repo_id", ""),
                    "repo_name": meta.get("repo_name", ""),
                    "crawl_date": meta.get("crawl_date", ""),
                    "crawl_ts": meta.get("crawl_ts", ""),
                    "stars": meta.get("stars", 0),
                    "today_stars": meta.get("today_stars", 0),
                    "since": meta.get("since", "daily"),
                    "rank": meta.get("rank", 0),
                    "url": meta.get("url", ""),
                    "content": doc[:1000],
                })

        return output

    def get_latest(self, repo_id: str = None, limit: int = 20) -> List[Dict]:
        """获取最新爬取的项目"""
        where = None
        if repo_id:
            where = {"repo_id": repo_id}

        results = self.collection.get(where=where)

        items = []
        if results["metadatas"]:
            for i, meta in enumerate(results["metadatas"]):
                items.append({
                    "repo_id": meta.get("repo_id"),
                    "repo_name": meta.get("repo_name"),
                    "crawl_date": meta.get("crawl_date"),
                    "crawl_ts": meta.get("crawl_ts", ""),
                    "stars": meta.get("stars"),
                    "today_stars": meta.get("today_stars", 0),
                    "since": meta.get("since", "daily"),
                    "rank": meta.get("rank", 0),
                    "url": meta.get("url"),
                    "content": results["documents"][i][:1000] if results["documents"] else "",
                })

        items.sort(key=lambda x: x.get("crawl_ts") or x.get("crawl_date", ""), reverse=True)
        return items[:limit]

def tool_rag_search(query: str, top_k: int = 10) -> str:
    """搜索项目"""
    store = RAGStore()
    results = store.search(query, n_results=top_k)

    if not results:
        return "未找到相关项目"

    output = [f"找到 {len(results)} 个相关项目：\n"]
    for r in results:
        output.append(
            f"- #{r.get('rank', 0)} {r['repo_name']} ({r['crawl_date']}, {r.get('since', 'daily')}) "
            f"今日+{r.get('today_stars', 0)} | 总⭐ {r['stars']}"
        )
        output.append(f"  {r['content'][:200]}...")
        output.append("")

    return "\n".join(output)

def tool_rag_get_latest(limit: int = 20) -> str:
    """获取最新项目"""
    store = RAGStore()
    results = store.get_latest(limit=limit)

    if not results:
        return "暂无数据，请先运行爬虫"

    output = [f"最新 {len(results)} 个项目：\n"]
    for r in results:
        output.append(
            f"- #{r.get('rank', 0)} {r['repo_name']} ({r['crawl_date']}, {r.get('since', 'daily')}) "
            f"今日+{r.get('today_stars', 0)} | 总⭐ {r['stars']}"
        )
        output.append(f"  {r['content'][:150]}")
        output.append("")

    return "\n".join(output)

def tool_rag_store(repo_id: str, repo_name: str, description: str, language: str,
                   stars: int, topics: list, url: str, readme: str = "",
                   since: str = "daily", today_stars: int = 0, rank: int = 0) -> str:
    """存储项目信息到 RAG"""
    from datetime import datetime
    store = RAGStore()

    project_data = {
        "repo_id": repo_id,
        "repo_name": repo_name,
        "description": description or "",
        "language": language or "",
        "stars": stars,
        "today_stars": today_stars,
        "since": since,
        "rank": rank,
        "topics": topics or [],
        "url": url,
        "crawl_date": datetime.now().strftime("%Y-%m-%d"),
        "crawl_ts": datetime.now().isoformat(timespec="seconds"),
        "readme": readme or "",
    }

    doc_id = store.add_project(project_data)
    return f"已存储项目: {repo_name} (ID: {doc_id[:8]}...)"


def tool_rag_reset() -> str:
    """删除并重建 Chroma 集合"""
    store = RAGStore()
    store.reset_collection()
    return "已删除并重建 Chroma 集合 projects"

RAG_TOOLS = [
    {
        "name": "rag_search",
        "description": "搜索已爬取的项目（语义搜索）",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词"},
                "top_k": {"type": "integer", "description": "返回数量", "default": 10}
            },
            "required": ["query"]
        }
    },
    {
        "name": "rag_get_latest",
        "description": "获取最新爬取的项目列表",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "返回数量", "default": 20}
            }
        }
    },
    {
        "name": "rag_store",
        "description": "将项目信息存入 RAG 系统",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo_id": {"type": "string", "description": "仓库唯一标识（如 owner/repo）"},
                "repo_name": {"type": "string", "description": "仓库名称"},
                "description": {"type": "string", "description": "仓库描述"},
                "language": {"type": "string", "description": "主要编程语言"},
                "stars": {"type": "integer", "description": "star 数量"},
                "topics": {"type": "array", "items": {"type": "string"}, "description": "主题标签"},
                "url": {"type": "string", "description": "仓库 URL"},
                "readme": {"type": "string", "description": "README 内容（可选）"},
                "since": {"type": "string", "description": "热榜周期：daily/weekly/monthly", "default": "daily"},
                "today_stars": {"type": "integer", "description": "趋势周期内新增 stars", "default": 0},
                "rank": {"type": "integer", "description": "榜单排名", "default": 0}
            },
            "required": ["repo_id", "repo_name", "stars", "url"]
        }
    },
    {
        "name": "rag_reset",
        "description": "删除并重建 Chroma 项目集合（用于全量刷新）",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    }
]

RAG_HANDLERS = {
    "rag_search": tool_rag_search,
    "rag_get_latest": tool_rag_get_latest,
    "rag_store": tool_rag_store,
    "rag_reset": tool_rag_reset,
}
