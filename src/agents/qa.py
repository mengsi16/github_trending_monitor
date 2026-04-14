"""问答 Agent (s02, s06)"""
from typing import List, Dict, Optional
from .base import BaseAgent
from ..tools import QA_TOOLS, QA_HANDLERS
from ..sessions import SQLiteSessionStore

# 基础 QA Prompt
QA_PROMPT_BASE = """你是一个 GitHub 热榜问答 Agent。

你可以使用以下工具：
- rag_search: 搜索已爬取的项目信息
- rag_get_latest: 获取最新爬取的项目列表
- request_crawl: 请求爬虫重新爬取 GitHub 热榜（当用户询问最新项目但没有数据时使用）
- get_agent_status: 查看当前系统状态

请根据用户的问题，搜索相关项目并给出回答。

如果 RAG 中没有相关数据，可以使用 request_crawl 工具请求爬虫更新数据。"""

# 不同性格的 System Prompt 模板
PERSONALITY_PROMPTS = {
    "tech": """你是一个技术团队的 AI 助手，专注于 GitHub 热榜项目的技术分析。

你擅长：
- 分析项目的技术栈和架构
- 评估代码质量和技术先进性
- 关注新兴技术和框架
- 深入讲解核心代码实现

回答风格：专业、深入、技术细节丰富。""",

    "invest": """你是一个投资团队的 AI 助手，专注于 GitHub 热榜项目的投资价值分析。

你擅长：
- 评估项目的商业潜力和市场价值
- 分析项目的用户增长和社区活跃度
- 关注创始团队背景和融资情况
- 评估项目的竞争壁垒和护城河

回答风格：商业化视角、简洁有力、重点突出投资价值。""",

    "content": """你是一个内容团队的 AI 助手，专注于 GitHub 热榜项目的内容创作。

你擅长：
- 提炼项目亮点和创新点
- 撰写吸引人的项目介绍
- 用通俗易懂的语言解释技术概念
- 策划项目推广内容

回答风格：生动有趣、通俗易懂、适合大众传播。""",

    "product": """你是一个产品团队的 AI 助手，专注于 GitHub 热榜项目的产品分析。

你擅长：
- 分析产品的功能特性和用户体验
- 评估产品的市场定位和目标用户
- 关注产品的迭代速度和方向
- 分析竞品对比和差异化

回答风格：产品视角、注重体验、关注用户价值。""",
}

def get_personality_prompt(personality: str = "tech") -> str:
    """获取指定性格的 System Prompt"""
    personality_part = PERSONALITY_PROMPTS.get(personality, PERSONALITY_PROMPTS["tech"])
    return f"{personality_part}\n\n{QA_PROMPT_BASE}"

class QAAgent(BaseAgent):
    """问答 Agent - 回答用户关于 GitHub 热榜的问题

    支持多 Bot 性格配置：
    - 通过 personality 参数指定性格 (tech/invest/content/product)
    - 不同性格有不同的 System Prompt
    """

    def __init__(self, name: str = "qa", session_store: SQLiteSessionStore = None,
                 personality: str = "tech"):
        """
        初始化 QAAgent

        Args:
            name: Agent 名称
            session_store: 可选的会话存储
            personality: 性格配置，可选 tech/invest/content/product
        """
        # 根据 personality 生成对应的 prompt
        prompt = get_personality_prompt(personality)
        super().__init__(name, prompt)
        self.tools = QA_TOOLS
        self.handlers = QA_HANDLERS
        self.personality = personality

        # 会话持久化
        self.session_store = session_store or SQLiteSessionStore(agent_id=name)
        self._current_session_id: Optional[str] = None

        # 内存中的会话历史（当前会话）
        self._session_messages: List[Dict] = []

    def _default_prompt(self) -> str:
        return get_personality_prompt(self.personality)

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
