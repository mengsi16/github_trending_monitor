"""问答 Agent (s02, s06)"""
from typing import List, Dict, Optional
from .base import BaseAgent
from ..tools import QA_TOOLS, QA_HANDLERS
from ..sessions import SQLiteSessionStore

QA_PROMPT = """你是一个 GitHub 热榜问答 Agent。

你可以使用以下工具：
- rag_search: 搜索已爬取的项目信息
- rag_get_latest: 获取最新爬取的项目列表
- request_crawl: 请求爬虫重新爬取 GitHub 热榜（当用户询问最新项目但没有数据时使用）
- get_agent_status: 查看当前系统状态

请根据用户的问题，搜索相关项目并给出回答。

如果 RAG 中没有相关数据，可以使用 request_crawl 工具请求爬虫更新数据。"""

class QAAgent(BaseAgent):
    """问答 Agent - 回答用户关于 GitHub 热榜的问题"""

    def __init__(self, name: str = "qa", session_store: SQLiteSessionStore = None):
        super().__init__(name, QA_PROMPT)
        self.tools = QA_TOOLS
        self.handlers = QA_HANDLERS

        # 会话持久化
        self.session_store = session_store or SQLiteSessionStore(agent_id=name)
        self._current_session_id: Optional[str] = None

        # 内存中的会话历史（当前会话）
        self._session_messages: List[Dict] = []

    def _default_prompt(self) -> str:
        return QA_PROMPT

    def create_session(self, title: str = None) -> str:
        """创建新会话"""
        session_id = self.session_store.create_session(title)
        self._current_session_id = session_id
        self._session_messages = []
        return session_id

    def load_session(self, session_id: str) -> bool:
        """加载指定会话"""
        info = self.session_store.get_session_info(session_id)
        if info is None:
            return False

        messages = self.session_store.load_session(session_id)
        self._current_session_id = session_id
        self._session_messages = messages
        return True

    def get_current_session_id(self) -> Optional[str]:
        """获取当前会话 ID"""
        return self._current_session_id

    def list_sessions(self, limit: int = 10) -> List[Dict]:
        """列出所有会话"""
        return self.session_store.list_sessions(limit)

    def save_current_session(self) -> None:
        """保存当前会话"""
        if self._current_session_id and self._session_messages:
            self.session_store.save_conversation(self._current_session_id, self._session_messages)

    def answer(self, question: str, session_id: str = None) -> str:
        """
        回答用户问题 - 支持会话持久化

        Args:
            question: 用户问题
            session_id: 可选的会话 ID，不提供则创建新会话
        """
        # 检查问题是否为空
        if not question or not question.strip():
            return "请输入有效的问题"

        # 处理会话
        if session_id:
            # 加载指定会话
            if session_id != self._current_session_id:
                self.load_session(session_id)
        elif not self._current_session_id:
            # 创建新会话
            self.create_session()

        # 获取历史消息
        messages = list(self._session_messages) if self._session_messages else None

        result, updated_messages = self.run(
            user_message=question,
            messages=messages,
            tools=self.tools,
            tool_handlers=self.handlers
        )

        # 更新会话历史
        self._session_messages = updated_messages

        # 自动保存到 SQLite
        if self._current_session_id:
            # 只保存新增的消息
            new_messages = updated_messages[len(messages) if messages else 0:]
            for msg in new_messages:
                self.session_store.save_message(
                    self._current_session_id,
                    msg["role"],
                    msg["content"]
                )

        return result

    def full_compact(self, keep_last: int = 1) -> None:
        """手动压缩 - 保留最近 N 轮对话"""
        self._session_messages = self.compactor.full_compact(
            self._session_messages,
            keep_last=keep_last
        )
