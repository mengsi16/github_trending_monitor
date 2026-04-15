"""Agent 间通信工具 - 让 QA Agent 可以请求其他 Agent"""
from typing import Optional


def _get_registry():
    from src.agents.registry import registry
    return registry


def tool_request_crawl(top_n: Optional[int] = None) -> str:
    """
    请求爬虫 Agent 重新爬取 GitHub 热榜

    当用户询问最新项目但 RAG 中没有数据时，可以调用此工具请求爬虫更新数据。

    Args:
        top_n: 可选，指定爬取数量。
    """
    print(f"[AgentTools] Requesting crawler to run (top_n={top_n})...")

    payload = {}
    if top_n is not None:
        payload["top_n"] = top_n

    # 通过 registry 调用 crawler
    registry = _get_registry()
    response = registry.call(
        from_agent="qa",
        to_agent="crawler",
        action="run_crawl",
        payload=payload
    )

    if response.success:
        result = response.result
        # 截取结果的关键部分
        if len(result) > 500:
            result = result[:500] + "\n... [结果已截断]"
        return f"爬虫已完成更新！\n\n{result}"
    else:
        return f"爬虫调用失败: {response.error}"


def tool_get_agent_status() -> str:
    """
    获取当前已注册的 Agent 状态
    """
    registry = _get_registry()
    agents = registry.list_agents()
    if not agents:
        return "暂无注册的 Agent"

    status_lines = ["已注册的 Agent:"]
    for name in agents:
        agent = registry.get(name)
        status_lines.append(f"- {name}: {type(agent).__name__}")

    return "\n".join(status_lines)


AGENT_TOOLS = [
    {
        "name": "request_crawl",
        "description": "请求爬虫 Agent 重新爬取 GitHub 热榜数据。"
                       '当用户询问"最新"、"今天"或 RAG 中没有相关数据时使用。',
        "input_schema": {
            "type": "object",
            "properties": {
                "top_n": {
                    "type": "integer",
                    "description": "可选，指定爬取数量（例如 30）。"
                }
            },
            "required": []
        }
    },
    {
        "name": "get_agent_status",
        "description": "获取当前系统中已注册的 Agent 列表和状态",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    }
]

AGENT_HANDLERS = {
    "request_crawl": tool_request_crawl,
    "get_agent_status": tool_get_agent_status,
}
