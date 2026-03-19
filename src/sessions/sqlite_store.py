"""SQLite 会话持久化存储 - 支持会话历史存储和恢复"""
import sqlite3
import json
import time
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime
import uuid

# 模块级 logger
_logger = logging.getLogger("sqlite_store")

# 使用 __file__ 计算项目根目录的路径，避免相对路径问题
WORKSPACE_DIR = Path(__file__).parent.parent.parent / "workspace"
SESSIONS_DIR = WORKSPACE_DIR / ".sessions"

def _ensure_sessions_dir():
    """延迟创建 sessions 目录，仅在首次使用时创建"""
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


class SQLiteSessionStore:
    """
    SQLite 持久化会话存储

    支持功能：
    1. 会话创建、加载、列表
    2. 自动保存对话历史
    3. 会话元数据管理（创建时间、最后活跃时间）
    4. 会话搜索
    """

    def __init__(self, agent_id: str = "default"):
        self.agent_id = agent_id
        _ensure_sessions_dir()  # 延迟创建目录
        self.db_path = SESSIONS_DIR / f"{agent_id}_sessions.db"
        self._init_db()

    def _init_db(self):
        """初始化数据库表"""
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()

        # 会话表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                title TEXT,
                created_at REAL,
                updated_at REAL,
                message_count INTEGER DEFAULT 0,
                metadata TEXT
            )
        """)

        # 消息表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp REAL NOT NULL,
                FOREIGN KEY (session_id) REFERENCES sessions(session_id)
            )
        """)

        # 创建索引
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_session
            ON messages(session_id)
        """)

        conn.commit()
        conn.close()

    def _get_conn(self):
        """获取数据库连接"""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def create_session(self, title: str = None) -> str:
        """创建新会话，返回 session_id"""
        session_id = str(uuid.uuid4())[:8]
        now = time.time()
        title = title or f"会话 {datetime.now().strftime('%Y-%m-%d %H:%M')}"

        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO sessions (session_id, title, created_at, updated_at, message_count) VALUES (?, ?, ?, ?, ?)",
            (session_id, title, now, now, 0)
        )
        conn.commit()
        conn.close()

        _logger.debug(f"Created session: {session_id}")
        return session_id

    def save_message(self, session_id: str, role: str, content: Any) -> None:
        """保存消息到会话"""
        now = time.time()

        # 处理 content 格式
        if isinstance(content, list):
            # 处理多部分内容（如工具调用结果）
            content_str = json.dumps(content, ensure_ascii=False)
        elif isinstance(content, dict):
            content_str = json.dumps(content, ensure_ascii=False)
        else:
            content_str = str(content)

        conn = self._get_conn()
        cursor = conn.cursor()

        # 保存消息
        cursor.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            (session_id, role, content_str, now)
        )

        # 更新会话的 message_count 和 updated_at
        cursor.execute(
            "UPDATE sessions SET message_count = message_count + 1, updated_at = ? WHERE session_id = ?",
            (now, session_id)
        )

        conn.commit()
        conn.close()

    def load_session(self, session_id: str) -> List[Dict]:
        """加载会话的所有消息"""
        conn = self._get_conn()
        cursor = conn.cursor()

        cursor.execute(
            "SELECT role, content, timestamp FROM messages WHERE session_id = ? ORDER BY timestamp ASC",
            (session_id,)
        )

        messages = []
        for row in cursor.fetchall():
            try:
                content = json.loads(row["content"])
            except (json.JSONDecodeError, TypeError):
                content = row["content"]

            messages.append({
                "role": row["role"],
                "content": content
            })

        conn.close()
        return messages

    def get_session_info(self, session_id: str) -> Optional[Dict]:
        """获取会话信息"""
        conn = self._get_conn()
        cursor = conn.cursor()

        cursor.execute(
            "SELECT session_id, title, created_at, updated_at, message_count FROM sessions WHERE session_id = ?",
            (session_id,)
        )

        row = cursor.fetchone()
        conn.close()

        if not row:
            return None

        return {
            "session_id": row["session_id"],
            "title": row["title"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "message_count": row["message_count"]
        }

    def list_sessions(self, limit: int = 20) -> List[Dict]:
        """列出所有会话（按最后活跃时间排序）"""
        conn = self._get_conn()
        cursor = conn.cursor()

        cursor.execute(
            "SELECT session_id, title, created_at, updated_at, message_count FROM sessions ORDER BY updated_at DESC LIMIT ?",
            (limit,)
        )

        sessions = []
        for row in cursor.fetchall():
            sessions.append({
                "session_id": row["session_id"],
                "title": row["title"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "message_count": row["message_count"]
            })

        conn.close()
        return sessions

    def update_session_title(self, session_id: str, title: str) -> None:
        """更新会话标题"""
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE sessions SET title = ?, updated_at = ? WHERE session_id = ?",
            (title, time.time(), session_id)
        )
        conn.commit()
        conn.close()

    def delete_session(self, session_id: str) -> bool:
        """删除会话"""
        conn = self._get_conn()
        cursor = conn.cursor()

        # 删除消息
        cursor.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))

        # 删除会话
        cursor.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))

        conn.commit()
        deleted = cursor.rowcount > 0
        conn.close()

        return deleted

    def save_conversation(self, session_id: str, messages: List[Dict]) -> None:
        """保存完整对话（批量操作）"""
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            self.save_message(session_id, role, content)

    def search_sessions(self, keyword: str, limit: int = 10) -> List[Dict]:
        """搜索包含关键词的会话"""
        conn = self._get_conn()
        cursor = conn.cursor()

        cursor.execute(
            """SELECT DISTINCT s.session_id, s.title, s.updated_at
               FROM sessions s
               JOIN messages m ON s.session_id = m.session_id
               WHERE m.content LIKE ?
               ORDER BY s.updated_at DESC
               LIMIT ?""",
            (f"%{keyword}%", limit)
        )

        sessions = []
        for row in cursor.fetchall():
            sessions.append({
                "session_id": row["session_id"],
                "title": row["title"],
                "updated_at": row["updated_at"]
            })

        conn.close()
        return sessions


# 便捷函数：创建默认的会话存储
def create_session_store(agent_id: str = "default") -> SQLiteSessionStore:
    """创建会话存储实例"""
    return SQLiteSessionStore(agent_id)
