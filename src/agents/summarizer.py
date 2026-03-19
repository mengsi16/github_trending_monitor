"""总结 Agent (s02, s05)"""
from .base import BaseAgent
from ..tools import RAG_TOOLS, RAG_HANDLERS, EMAIL_TOOLS, EMAIL_HANDLERS, FEISHU_TOOLS, FEISHU_HANDLERS
from src.config import config

# 团队风格定义 - 作为系统提示词的一部分
TEAM_STYLES = {
    "tech": {
        "name": "技术团队",
        "system": """你是一个技术团队的技术编辑。
你的风格：专业、严谨、技术细节丰富、实用建议
输出要求：
- 使用技术术语
- 关注技术栈、架构设计、代码质量
- 提供学习路径和实践建议
- 输出格式：简洁的技术摘要，适度使用表格""",
        "example": """## 技术要点分析

**1. 架构亮点**
- 项目使用 X 技术栈，采用 Y 架构
- 关键创新点：...

**2. 学习建议**
- 入门建议：...
- 进阶方向：..."""
    },
    "invest": {
        "name": "投资团队",
        "system": """你是一个投资分析师。
你的风格：数据驱动、商业分析、风险评估
输出要求：
- 关注增长趋势、商业潜力、投资回报
- 使用数据表格呈现关键指标
- 提供明确的行动建议
- 输出格式：投资分析报告""",
        "example": """## 投资分析

### 核心指标
| 项目 | 增长潜力 | 风险等级 | 建议 |
|------|----------|----------|------|
| xxx  | 高       | 中       | 关注  |

### 赛道分析
..."""
    },
    "content": {
        "name": "内容团队",
        "system": """你是一个内容创作者。
你的风格：生动、有趣、适合短视频传播
输出要求：
- 关注热点话题、传播性、视觉素材
- 使用 emoji 增添趣味
- 提供短视频选题建议
- 输出格式：轻松的热点速览""",
        "example": """🔥 今日热点

### 必看项目
1️⃣ **项目名** - 超有趣的点：...
📹 短视频选题：...

💡 传播亮点：..."""
    },
    "product": {
        "name": "产品团队",
        "system": """你是一个产品经理。
你的风格：产品视角、用户体验、竞品分析
输出要求：
- 关注功能创新、用户体验、市场定位
- 分析产品差异化和竞争力
- 提供产品优化建议
- 输出格式：产品分析报告""",
        "example": """## 产品分析

### 核心功能
- 功能亮点：...
- 用户价值：...

### 竞品对标
| 维度 | 本项目 | 竞品A |
|------|--------|-------|
| 功能 | xxx    | yyy   |

### 产品建议
..."""
    },
}

SUMMARIZER_PROMPT = """你是一个 GitHub 热榜总结 Agent。

你的任务是根据已爬取的项目信息，按团队生成每日总结。

重要：你会收到团队类型，请使用该团队对应的风格来生成总结。
如果用户没有指定团队，默认使用技术团队风格。"""

class SummarizerAgent(BaseAgent):
    """总结 Agent - 按团队生成不同风格的总结"""

    def __init__(self, name: str = "summarizer"):
        super().__init__(name, SUMMARIZER_PROMPT)
        self.tools = RAG_TOOLS + EMAIL_TOOLS + FEISHU_TOOLS
        self.handlers = {**RAG_HANDLERS, **EMAIL_HANDLERS, **FEISHU_HANDLERS}

    def _default_prompt(self) -> str:
        return SUMMARIZER_PROMPT

    def _get_team_system_prompt(self, team_id: str) -> str:
        """获取团队特定的系统提示词"""
        team_style = TEAM_STYLES.get(team_id)
        if not team_style:
            team_style = TEAM_STYLES["tech"]

        # 组合基础提示词和团队风格
        return f"""{SUMMARIZER_PROMPT}

## 当前团队风格：{team_style['name']}

{team_style['system']}

输出示例参考：
{team_style['example']}"""

    def generate_summary(self, team_id: str) -> str:
        """为指定团队生成总结 - 使用团队特定的风格"""
        # 获取团队特定的系统提示词
        team_system_prompt = self._get_team_system_prompt(team_id)
        team_style = TEAM_STYLES.get(team_id, TEAM_STYLES["tech"])

        user_msg = f"""请为 {team_style['name']} 生成今日 GitHub 热榜总结。

请先调用 rag_get_latest 获取最新项目，然后生成针对性的总结。

要求：
1. 必须使用上述团队风格来输出
2. 输出示例中的格式可以作为参考
3. 内容要专业且有针对性"""

        # 复用 BaseAgent.run()，传入团队特定的系统提示词
        text, _ = self.run(
            user_message=user_msg,
            messages=None,
            tools=self.tools,
            tool_handlers=self.handlers,
            override_system_prompt=team_system_prompt
        )
        return text

    def generate_all_summaries(self) -> dict:
        """为所有团队生成总结"""
        results = {}
        for team in config.teams:
            try:
                summary = self.generate_summary(team.id)
                results[team.id] = summary
            except Exception as e:
                results[team.id] = f"生成失败: {e}"
        return results
