"""ChromaDB RAG 工具 (s06)"""
import chromadb
from chromadb.config import Settings
from typing import List, Dict, Any
import uuid

class RAGStore:
    def __init__(self, persist_dir: str = None):
        from src.config import config
        self.persist_dir = persist_dir or config.chromadb_dir
        self.client = chromadb.PersistentClient(path=self.persist_dir)
        self.collection = self.client.get_or_create_collection(
            name="projects",
            metadata={"description": "GitHub trending projects"}
        )

    def add_project(self, project_data: Dict) -> str:
        """添加项目记录（保留历史）"""
        doc_id = str(uuid.uuid4())

        doc_content = f"""
        项目: {project_data.get('repo_name')}
        描述: {project_data.get('description', '')}
        语言: {project_data.get('language', '')}
        Stars: {project_data.get('stars', 0)}
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
                "stars": project_data.get("stars", 0),
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
                    "stars": meta.get("stars", 0),
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
                    "stars": meta.get("stars"),
                    "url": meta.get("url"),
                    "content": results["documents"][i][:1000] if results["documents"] else "",
                })

        items.sort(key=lambda x: x.get("crawl_date", ""), reverse=True)
        return items[:limit]

def tool_rag_search(query: str, top_k: int = 10) -> str:
    """搜索项目"""
    store = RAGStore()
    results = store.search(query, n_results=top_k)

    if not results:
        return "未找到相关项目"

    output = [f"找到 {len(results)} 个相关项目：\n"]
    for r in results:
        output.append(f"- {r['repo_name']} ({r['crawl_date']}) ⭐ {r['stars']}")
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
        output.append(f"- {r['repo_name']} ({r['crawl_date']}) ⭐ {r['stars']}")
        output.append(f"  {r['content'][:150]}")
        output.append("")

    return "\n".join(output)

def tool_rag_store(repo_id: str, repo_name: str, description: str, language: str,
                   stars: int, topics: list, url: str, readme: str = "") -> str:
    """存储项目信息到 RAG"""
    from datetime import datetime
    store = RAGStore()

    project_data = {
        "repo_id": repo_id,
        "repo_name": repo_name,
        "description": description or "",
        "language": language or "",
        "stars": stars,
        "topics": topics or [],
        "url": url,
        "crawl_date": datetime.now().strftime("%Y-%m-%d"),
        "readme": readme or "",
    }

    doc_id = store.add_project(project_data)
    return f"已存储项目: {repo_name} (ID: {doc_id[:8]}...)"

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
                "readme": {"type": "string", "description": "README 内容（可选）"}
            },
            "required": ["repo_id", "repo_name", "stars", "url"]
        }
    }
]

RAG_HANDLERS = {
    "rag_search": tool_rag_search,
    "rag_get_latest": tool_rag_get_latest,
    "rag_store": tool_rag_store,
}
