"""问答 Agent (s02, s06)"""
import threading
from typing import List, Dict, Optional
from .base import BaseAgent
from ..tools import QA_TOOLS, QA_HANDLERS
from ..tools.skills import get_skill_descriptions
from ..sessions import SQLiteSessionStore

# 基础 QA Prompt
QA_PROMPT_BASE = """你是一个 GitHub 热榜问答 Agent，拥有丰富的工具链来获取和整合信息。

## 可用工具

### brain-base 知识库（最优先使用）
- brain_base_health: 检查 brain-base 是否可用（首次调用较慢需 30~60s 加载模型，结果缓存 5 分钟）
- brain_base_search: 搜索 brain-base 知识库（Milvus 向量检索），返回匹配文档片段
- brain_base_ask: 向 brain-base 提问（Agentic RAG），返回完整回答
- brain_base_exists: 检查 URL 是否已在知识库中
- brain_base_ingest_url: 将新 URL 入库到 brain-base

**使用前先自检**：新会话的第一次问答，先调用 brain_base_health 确认可用。
- 若返回"可用"：优先调 brain_base_search / brain_base_ask 获取答案
- 若返回"不可用"：**不要再尝试其他 brain_base_* 工具**，直接降级到 rag_search / rag_get_latest，必要时用 browser_navigate 补充
- 后续同一会话内无需反复 health，5 分钟内走缓存

### Skills（按需加载）
- load_skill: 按名称加载一个 Skill 的完整指令
- list_skills: 列出当前已发现的 Skills

当前可用 Skills:
{skill_descriptions}

### ChromaDB 数据检索（brain-base 降级后使用）
- rag_search: 搜索已爬取的项目信息（语义搜索）
- rag_get_latest: 获取最新爬取的项目列表
- rag_analyze_changes: 分析项目内容变化

### 网页研究（brain-base 和 RAG 都无数据时使用）
- browser_navigate: 用 Playwright 浏览器访问网页，获取实时信息
- browser_snapshot: 获取当前页面快照
- browser_click / browser_type: 与页面交互（点击、输入）
- browser_take_screenshot: 截图页面内容
- 其他 browser_* 工具: 浏览器自动化操作

### 本地文件访问
- read_file: 读取项目目录下的文件（包括 .playwright-mcp/ 下的页面快照）
- list_dir: 列出目录内容，发现可用文件
- grep: 在文件中搜索文本模式
- bash: 执行 Shell 命令（PowerShell/Bash，有安全限制）

### 系统操作
- request_crawl: 请求爬虫重新爬取 GitHub 热榜
- get_agent_status: 查看系统状态
- memory_snapshot: 查看进程内存状态

## 信息获取策略（按优先级）

1. **brain-base 优先**: 先调 brain_base_search 或 brain_base_ask 获取答案
2. **RAG 降级**: brain-base 无结果或失败时，用 rag_search / rag_get_latest
3. **检查 Playwright 缓存**: Playwright 每次浏览网页后，会自动将页面快照保存到
   `.playwright-mcp/` 目录下（yml 格式的 accessibility snapshot）。
   在发起新的浏览器访问之前，先用 grep 搜索 `.playwright-mcp/` 目录，
   看是否已有相关页面的缓存数据。例如：
   - grep "cc-switch" ".playwright-mcp" → 查找包含 cc-switch 的页面快照
   - list_dir ".playwright-mcp" → 查看有哪些快照文件
   - read_file ".playwright-mcp/page-xxx.yml" → 读取具体页面内容
   这样可以避免重复浏览，节省时间和资源。
4. **Playwright 补充**: 如果 brain-base 和 RAG 都没有相关数据，
   用 browser_navigate 访问该项目的 GitHub 页面获取实时信息。
   - 访问 https://github.com/{{owner}}/{{repo}} 获取 README、Stars、语言等
   - 访问 https://github.com/{{owner}}/{{repo}}/issues 或 /pulls 获取社区活跃度
   - 访问竞品对比页面获取对比信息
   - 浏览完成后，新页面快照会自动保存到 .playwright-mcp/ 供后续读取
5. **请求爬虫**: 如果需要更新整个热榜数据，用 request_crawl

## 重要原则

- **brain-base 优先于 RAG**：必须先调 brain_base_search/ask，不要跳过直接用 rag_search
- 不要仅因为 brain-base 和 RAG 都没数据就放弃回答，主动用 Playwright 去网页获取信息
- **先查缓存再浏览**: 用 grep 搜索 .playwright-mcp/ 目录，避免重复访问同一页面
- 比较类问题（如"A 和 B 的区别"）：分别获取两个项目的信息后再对比
- 每次浏览器访问后，用 browser_snapshot 确认页面内容，避免盲目操作
- 如果浏览器访问失败，说明原因并基于已有信息给出最佳回答"""

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
    skill_desc = get_skill_descriptions()
    base = QA_PROMPT_BASE.format(skill_descriptions=skill_desc)
    return f"{personality_part}\n\n{base}"

class QAAgent(BaseAgent):
    """问答 Agent - 回答用户关于 GitHub 热榜的问题

    支持多 Bot 性格配置：
    - 通过 personality 参数指定性格 (tech/invest/content/product)
    - 不同性格有不同的 System Prompt

    支持多通道会话隔离：
    - 每个 channel:peer_id 组合拥有独立的会话上下文
    - CLI 和飞书的消息历史互不干扰
    - 线程安全：通过 _lock 保护并发访问
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

        # 多通道会话隔离：每个 channel:peer_id 拥有独立的会话上下文
        # key = "channel:peer_id" (e.g. "cli:default", "feishu:oc_xxx")
        # value = {"session_id": str, "messages": list}
        self._channel_contexts: Dict[str, Dict] = {}

        # 线程安全锁
        self._lock = threading.Lock()

        # 向后兼容：CLI 单会话模式的属性
        self._current_session_id: Optional[str] = None
        self._session_messages: List[Dict] = []

    def _default_prompt(self) -> str:
        return get_personality_prompt(self.personality)

    def _get_context_key(self, channel: str = None, peer_id: str = None) -> str:
        """生成通道上下文的唯一 key

        Args:
            channel: 通道名称 (cli/feishu/email)
            peer_id: 对端 ID (飞书群 ID / 用户 ID)
        """
        if channel and peer_id:
            return f"{channel}:{peer_id}"
        elif channel:
            return f"{channel}:default"
        else:
            return "cli:default"

    def _get_or_create_context(self, context_key: str) -> Dict:
        """获取或创建通道上下文"""
        if context_key not in self._channel_contexts:
            session_id = self.session_store.create_session(
                title=f"会话 [{context_key}]"
            )
            self._channel_contexts[context_key] = {
                "session_id": session_id,
                "messages": [],
            }
        return self._channel_contexts[context_key]

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

    def answer(self, question: str, session_id: str = None,
               channel: str = None, peer_id: str = None) -> str:
        """
        回答用户问题 - 支持会话持久化和多通道隔离

        Args:
            question: 用户问题
            session_id: 可选的会话 ID，不提供则创建新会话
            channel: 通道名称 (cli/feishu/email)，用于会话隔离
            peer_id: 对端 ID，用于同通道内的不同会话隔离
        """
        # 检查问题是否为空
        if not question or not question.strip():
            return "请输入有效的问题"

        # 多通道隔离模式：如果提供了 channel，使用独立的上下文
        if channel:
            return self._answer_isolated(question, channel, peer_id)

        # 向后兼容：CLI 单会话模式（无 channel 参数时走旧逻辑）
        return self._answer_legacy(question, session_id)

    def _answer_isolated(self, question: str, channel: str, peer_id: str = None) -> str:
        """多通道隔离模式：每个通道拥有独立的会话上下文"""
        context_key = self._get_context_key(channel, peer_id)

        with self._lock:
            ctx = self._get_or_create_context(context_key)
            session_id = ctx["session_id"]
            messages = list(ctx["messages"]) if ctx["messages"] else None

        result, updated_messages = self.run(
            user_message=question,
            messages=messages,
            tools=self.tools,
            tool_handlers=self.handlers
        )

        with self._lock:
            # 重新获取上下文（防止并发修改）
            ctx = self._get_or_create_context(context_key)
            ctx["messages"] = updated_messages

            # 同步到向后兼容属性（供 CLI 的 /compact, /sessions 等命令使用）
            self._current_session_id = ctx["session_id"]
            self._session_messages = updated_messages

            # 自动保存到 SQLite
            new_messages = updated_messages[len(messages) if messages else 0:]
            for msg in new_messages:
                self.session_store.save_message(
                    ctx["session_id"],
                    msg["role"],
                    msg["content"]
                )

        return result

    def _answer_legacy(self, question: str, session_id: str = None) -> str:
        """向后兼容的 CLI 单会话模式"""
        with self._lock:
            # 处理会话
            if session_id:
                if session_id != self._current_session_id:
                    self.load_session(session_id)
            elif not self._current_session_id:
                self.create_session()

            messages = list(self._session_messages) if self._session_messages else None

        result, updated_messages = self.run(
            user_message=question,
            messages=messages,
            tools=self.tools,
            tool_handlers=self.handlers
        )

        with self._lock:
            self._session_messages = updated_messages

            # 自动保存到 SQLite
            if self._current_session_id:
                new_messages = updated_messages[len(messages) if messages else 0:]
                for msg in new_messages:
                    self.session_store.save_message(
                        self._current_session_id,
                        msg["role"],
                        msg["content"]
                    )

        return result

    def full_compact(self, keep_last: int = 1) -> None:
        """手动压缩 - 保留最近 N 轮对话（压缩当前活跃的上下文）"""
        with self._lock:
            self._session_messages = self.compactor.full_compact(
                self._session_messages,
                keep_last=keep_last
            )
            # 同步到通道上下文
            for ctx in self._channel_contexts.values():
                if ctx["session_id"] == self._current_session_id:
                    ctx["messages"] = self._session_messages
                    break
