"""ChromaDB RAG 工具 (s06)"""
import chromadb
import hashlib
import json
import re
from chromadb.config import Settings
from typing import List, Dict, Any, Optional
from datetime import datetime

README_MAX_CHARS = 8000
QUERY_STOP_WORDS = {
    "github", "热榜", "项目", "仓库", "repo", "repository", "请问", "帮我", "分析", "介绍",
    "一下", "看看", "哪些", "什么", "怎么", "最近", "这个", "那个", "关于", "今天",
}

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

    def _build_doc_content(self, project_data: Dict) -> str:
        readme = (project_data.get('readme', '') or '')[:README_MAX_CHARS]
        return f"""
        项目: {project_data.get('repo_name')}
        描述: {project_data.get('description', '')}
        语言: {project_data.get('language', '')}
        总 Stars: {project_data.get('stars', 0)}
        趋势新增 Stars: {project_data.get('today_stars', 0)}
        热榜周期: {project_data.get('since', 'daily')}
        榜单名次: {project_data.get('rank', 0)}
        主题: {', '.join(project_data.get('topics', []))}
        README: {readme}
        """.strip()

    def _build_content_signature(self, project_data: Dict) -> str:
        readme = (project_data.get("readme", "") or "")[:README_MAX_CHARS]
        signature_payload = {
            "repo_id": project_data.get("repo_id"),
            "repo_name": project_data.get("repo_name"),
            "description": project_data.get("description", "") or "",
            "language": project_data.get("language", "") or "",
            "topics": sorted(project_data.get("topics", []) or []),
            "url": project_data.get("url", "") or "",
            "readme": readme,
        }
        raw = json.dumps(signature_payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _build_readme_signature(self, project_data: Dict) -> str:
        readme = (project_data.get("readme", "") or "")[:README_MAX_CHARS]
        if not readme:
            return ""
        return hashlib.sha256(readme.encode("utf-8")).hexdigest()

    def _build_latest_doc_id(self, repo_id: str) -> str:
        return f"latest::{repo_id}"

    def _build_snapshot_doc_id(self, repo_id: str, content_signature: str) -> str:
        return f"snapshot::{repo_id}::{content_signature[:16]}"

    def _build_metadata(self, project_data: Dict, content_signature: str, record_type: str, change_status: str) -> Dict[str, Any]:
        topics = sorted(project_data.get("topics", []) or [])
        return {
            "repo_id": project_data.get("repo_id"),
            "repo_name": project_data.get("repo_name"),
            "crawl_date": project_data.get("crawl_date"),
            "crawl_ts": project_data.get("crawl_ts"),
            "crawl_batch_id": project_data.get("crawl_batch_id", ""),
            "content_signature": content_signature,
            "record_type": record_type,
            "change_status": change_status,
            "description": project_data.get("description", "") or "",
            "language": project_data.get("language", "") or "",
            "topics_text": ", ".join(topics),
            "readme_signature": self._build_readme_signature(project_data),
            "stars": project_data.get("stars", 0),
            "today_stars": project_data.get("today_stars", 0),
            "since": project_data.get("since", "daily"),
            "rank": project_data.get("rank", 0),
            "url": project_data.get("url", ""),
        }

    def _get_record_by_id(self, doc_id: str) -> Optional[Dict[str, Any]]:
        results = self.collection.get(ids=[doc_id])
        ids = results.get("ids") or []
        if not ids:
            return None

        return {
            "id": ids[0],
            "document": (results.get("documents") or [""])[0],
            "metadata": (results.get("metadatas") or [{}])[0],
        }

    def _list_records(self, where: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        results = self.collection.get(where=where)
        ids = results.get("ids") or []
        documents = results.get("documents") or []
        metadatas = results.get("metadatas") or []
        records = []
        for i, metadata in enumerate(metadatas):
            records.append({
                "id": ids[i] if i < len(ids) else "",
                "document": documents[i] if i < len(documents) else "",
                "metadata": metadata,
            })
        return records

    def _sort_records_by_crawl_ts(self, records: List[Dict[str, Any]], reverse: bool = False) -> List[Dict[str, Any]]:
        return sorted(
            records,
            key=lambda record: (
                record.get("metadata", {}).get("crawl_ts") or record.get("metadata", {}).get("crawl_date") or "",
                record.get("metadata", {}).get("repo_name") or "",
            ),
            reverse=reverse,
        )

    def _get_previous_snapshot(self, repo_id: str, current_signature: str) -> Optional[Dict[str, Any]]:
        snapshots = self._sort_records_by_crawl_ts(
            self._list_records({"repo_id": repo_id, "record_type": "snapshot"}),
            reverse=True,
        )
        seen_current = False
        for snapshot in snapshots:
            signature = snapshot.get("metadata", {}).get("content_signature", "")
            if signature == current_signature and not seen_current:
                seen_current = True
                continue
            if signature != current_signature:
                return snapshot
        return None

    def _describe_content_changes(self, current_meta: Dict[str, Any], previous_meta: Optional[Dict[str, Any]]) -> List[str]:
        if not previous_meta:
            return ["首次收录"]

        changes = []
        if (current_meta.get("description") or "") != (previous_meta.get("description") or ""):
            changes.append("描述已更新")
        if (current_meta.get("language") or "") != (previous_meta.get("language") or ""):
            before = previous_meta.get("language") or "未知"
            after = current_meta.get("language") or "未知"
            changes.append(f"语言 {before} -> {after}")
        if (current_meta.get("topics_text") or "") != (previous_meta.get("topics_text") or ""):
            changes.append("主题标签已变化")
        if (current_meta.get("readme_signature") or "") != (previous_meta.get("readme_signature") or ""):
            changes.append("README 已更新")
        return changes or ["内容有变更"]

    def get_change_report(self, repo_id: str = "", limit: int = 10) -> Dict[str, Any]:
        latest_records = self._sort_records_by_crawl_ts(self._list_records({"record_type": "latest"}), reverse=True)
        if not latest_records:
            return {"items": [], "summary": {"new": 0, "updated": 0, "unchanged": 0}, "latest_batch_id": ""}

        if repo_id:
            latest_record = next((record for record in latest_records if record.get("metadata", {}).get("repo_id") == repo_id), None)
            if not latest_record:
                return {"items": [], "summary": {"new": 0, "updated": 0, "unchanged": 0}, "latest_batch_id": ""}

            current_meta = latest_record.get("metadata", {})
            status = current_meta.get("change_status", "unknown")
            previous_snapshot = self._get_previous_snapshot(repo_id, current_meta.get("content_signature", ""))
            item = {
                "repo_id": repo_id,
                "repo_name": current_meta.get("repo_name", repo_id),
                "status": status,
                "crawl_date": current_meta.get("crawl_date", ""),
                "crawl_batch_id": current_meta.get("crawl_batch_id", ""),
                "changes": self._describe_content_changes(current_meta, previous_snapshot.get("metadata") if previous_snapshot else None),
            }
            return {
                "items": [item],
                "summary": {"new": 1 if status == "new" else 0, "updated": 1 if status == "updated" else 0, "unchanged": 1 if status == "unchanged" else 0},
                "latest_batch_id": current_meta.get("crawl_batch_id", ""),
            }

        latest_batch_id = max((record.get("metadata", {}).get("crawl_batch_id") or "" for record in latest_records), default="")
        batch_records = [record for record in latest_records if (record.get("metadata", {}).get("crawl_batch_id") or "") == latest_batch_id]
        batch_records = self._sort_records_by_crawl_ts(batch_records)

        summary = {"new": 0, "updated": 0, "unchanged": 0}
        items = []
        for record in batch_records:
            current_meta = record.get("metadata", {})
            status = current_meta.get("change_status", "unknown")
            if status in summary:
                summary[status] += 1
            if status == "unchanged":
                continue

            previous_snapshot = self._get_previous_snapshot(current_meta.get("repo_id", ""), current_meta.get("content_signature", ""))
            items.append({
                "repo_id": current_meta.get("repo_id", ""),
                "repo_name": current_meta.get("repo_name", ""),
                "status": status,
                "crawl_date": current_meta.get("crawl_date", ""),
                "crawl_batch_id": current_meta.get("crawl_batch_id", ""),
                "changes": self._describe_content_changes(current_meta, previous_snapshot.get("metadata") if previous_snapshot else None),
            })

        items.sort(key=lambda item: (item.get("status") != "updated", item.get("repo_name") or ""))
        return {"items": items[:limit], "summary": summary, "latest_batch_id": latest_batch_id}

    def add_project(self, project_data: Dict) -> Dict[str, Any]:
        """添加项目记录（保留历史）"""
        repo_id = project_data.get("repo_id")
        if not repo_id:
            raise ValueError("repo_id 不能为空")

        doc_content = self._build_doc_content(project_data)
        content_signature = self._build_content_signature(project_data)
        latest_doc_id = self._build_latest_doc_id(repo_id)
        snapshot_doc_id = self._build_snapshot_doc_id(repo_id, content_signature)

        existing_latest = self._get_record_by_id(latest_doc_id)
        previous_signature = ""
        if existing_latest:
            previous_signature = existing_latest["metadata"].get("content_signature", "")

        if not existing_latest:
            status = "new"
        elif previous_signature == content_signature:
            status = "unchanged"
        else:
            status = "updated"

        latest_metadata = self._build_metadata(project_data, content_signature, "latest", status)
        snapshot_metadata = self._build_metadata(project_data, content_signature, "snapshot", status)

        self.collection.upsert(
            documents=[doc_content],
            ids=[latest_doc_id],
            metadatas=[latest_metadata],
        )

        snapshot_added = False
        if status != "unchanged" and not self._get_record_by_id(snapshot_doc_id):
            self.collection.add(
                documents=[doc_content],
                ids=[snapshot_doc_id],
                metadatas=[snapshot_metadata],
            )
            snapshot_added = True

        return {
            "id": latest_doc_id,
            "status": status,
            "snapshot_added": snapshot_added,
            "content_signature": content_signature,
        }

    def search(self, query: str, n_results: int = 10,
               where: Dict = None) -> List[Dict]:
        """语义搜索项目"""
        effective_where = {"record_type": "latest"}
        if where:
            effective_where.update(where)

        results = self.collection.query(
            query_texts=[query],
            n_results=n_results,
            where=effective_where
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
        where = {"record_type": "latest"}
        if repo_id:
            where["repo_id"] = repo_id

        results = self.collection.get(where=where)

        items = []
        if results["metadatas"]:
            for i, meta in enumerate(results["metadatas"]):
                items.append({
                    "repo_id": meta.get("repo_id"),
                    "repo_name": meta.get("repo_name"),
                    "crawl_date": meta.get("crawl_date"),
                    "crawl_ts": meta.get("crawl_ts", ""),
                    "crawl_batch_id": meta.get("crawl_batch_id", ""),
                    "stars": meta.get("stars"),
                    "today_stars": meta.get("today_stars", 0),
                    "since": meta.get("since", "daily"),
                    "rank": meta.get("rank", 0),
                    "url": meta.get("url"),
                    "content": results["documents"][i][:1000] if results["documents"] else "",
                })

        if not items:
            return []

        latest_batch_id = max((item.get("crawl_batch_id") or "" for item in items), default="")
        if latest_batch_id:
            items = [item for item in items if item.get("crawl_batch_id") == latest_batch_id]
        else:
            latest_marker = max(
                (item.get("crawl_ts") or item.get("crawl_date") or "" for item in items),
                default="",
            )
            items = [
                item
                for item in items
                if (item.get("crawl_ts") or item.get("crawl_date") or "") == latest_marker
            ]

        items.sort(key=lambda x: (x.get("rank") or 0, x.get("repo_name") or ""))
        return items[:limit]


def _rewrite_query_variants(query: str) -> List[str]:
    base_query = (query or "").strip()
    if not base_query:
        return []

    variants = [base_query]
    repo_matches = re.findall(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", base_query)
    variants.extend(repo_matches)

    normalized = re.sub(r"[^0-9A-Za-z_./\-\u4e00-\u9fff]+", " ", base_query).strip()
    if normalized and normalized != base_query:
        variants.append(normalized)

    keyword_tokens = []
    for token in re.split(r"\s+", normalized or base_query):
        cleaned = token.strip().lower()
        if not cleaned or cleaned in QUERY_STOP_WORDS:
            continue
        if len(cleaned) == 1 and "/" not in cleaned:
            continue
        keyword_tokens.append(token.strip())

    if keyword_tokens:
        variants.append(" ".join(keyword_tokens[:6]))
        for token in keyword_tokens[:4]:
            variants.append(token)

    deduped = []
    seen = set()
    for variant in variants:
        item = variant.strip()
        if not item:
            continue
        lowered = item.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        deduped.append(item)
    return deduped[:6]

def tool_rag_search(query: str, top_k: int = 10) -> str:
    """搜索项目"""
    store = RAGStore()
    variants = _rewrite_query_variants(query)
    merged_results = []
    seen_repo_ids = set()

    for variant in variants or [query]:
        results = store.search(variant, n_results=top_k)
        for result in results:
            repo_id = result.get("repo_id") or result.get("repo_name")
            if not repo_id or repo_id in seen_repo_ids:
                continue
            seen_repo_ids.add(repo_id)
            merged_results.append(result)
            if len(merged_results) >= top_k:
                break
        if len(merged_results) >= top_k:
            break

    if not merged_results:
        return "未找到相关项目"

    output = [f"找到 {len(merged_results)} 个相关项目：\n"]
    if len(variants) > 1:
        output.append(f"查询改写: {' | '.join(variants[:3])}")
        output.append("")
    for r in merged_results:
        output.append(
            f"- #{r.get('rank', 0)} {r['repo_name']} ({r['crawl_date']}, {r.get('since', 'daily')}) "
            f"今日+{r.get('today_stars', 0)} | 总⭐ {r['stars']}"
        )
        output.append(f"  {r['content'][:200]}...")
        output.append("")

    return "\n".join(output)


def tool_rag_analyze_changes(repo_id: str = "", limit: int = 10) -> str:
    """分析项目内容变化"""
    store = RAGStore()
    report = store.get_change_report(repo_id=repo_id, limit=limit)
    items = report.get("items", [])
    summary = report.get("summary", {})
    latest_batch_id = report.get("latest_batch_id", "")

    if repo_id:
        if not items:
            return f"未找到项目: {repo_id}"

        item = items[0]
        lines = [
            f"项目变化分析: {item['repo_name']}",
            f"最新批次: {item.get('crawl_batch_id') or latest_batch_id or '未知'}",
            f"状态: {item.get('status', 'unknown')}",
            "变化点:",
        ]
        for change in item.get("changes", []):
            lines.append(f"- {change}")
        return "\n".join(lines)

    if not latest_batch_id:
        return "暂无数据，请先运行爬虫"

    lines = [
        f"最近批次项目变化分析: {latest_batch_id}",
        f"新增: {summary.get('new', 0)} | 内容更新: {summary.get('updated', 0)} | 内容未变: {summary.get('unchanged', 0)}",
        "",
    ]

    if not items:
        lines.append("本批次没有内容变化的项目。")
        return "\n".join(lines)

    lines.append("变化项目:")
    for item in items:
        changes = "；".join(item.get("changes", []))
        lines.append(f"- {item['repo_name']} [{item.get('status', 'unknown')}] {changes}")
    return "\n".join(lines)

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
        "crawl_batch_id": datetime.now().isoformat(timespec="microseconds"),
        "readme": readme or "",
    }

    result = store.add_project(project_data)
    return f"已存储项目: {repo_name} (状态: {result['status']}, ID: {result['id']})"


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
        "name": "rag_analyze_changes",
        "description": "分析项目内容变化。可传 repo_id 查看单个项目变化，不传则分析最近一批发生内容变化的项目。",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo_id": {"type": "string", "description": "仓库唯一标识（如 owner/repo）", "default": ""},
                "limit": {"type": "integer", "description": "最近批次最多返回多少个变化项目", "default": 10}
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
    "rag_analyze_changes": tool_rag_analyze_changes,
    "rag_store": tool_rag_store,
    "rag_reset": tool_rag_reset,
}
