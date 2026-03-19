"""Agent 注册与通信机制 - 支持 Agent 之间相互调用"""
from typing import Dict, Any, Optional, Callable
import time
import threading
from enum import Enum


class AgentRequest:
    """Agent 间请求"""

    def __init__(self, request_id: str, from_agent: str, to_agent: str,
                 action: str, payload: Dict[str, Any] = None):
        self.request_id = request_id
        self.from_agent = from_agent
        self.to_agent = to_agent
        self.action = action
        self.payload = payload or {}
        self.timestamp = time.time()
        self.status = "pending"


class AgentResponse:
    """Agent 间响应"""

    def __init__(self, request_id: str, success: bool, result: Any = None, error: str = None):
        self.request_id = request_id
        self.success = success
        self.result = result
        self.error = error
        self.timestamp = time.time()


class AgentRegistry:
    """
    Agent 注册中心 - 管理所有 Agent 实例，支持 Agent 间通信

    使用方式:
    1. 注册 Agent: registry.register("qa", qa_agent)
    2. 获取 Agent: qa_agent = registry.get("qa")
    3. Agent 间调用: registry.call("qa", "crawler", "run_crawl", {})
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        """单例模式"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._agents: Dict[str, Any] = {}
        self._handlers: Dict[str, Callable] = {}  # action -> handler
        self._request_handlers: Dict[str, Callable] = {}  # agent_name -> handler
        self._initialized = True

    def register(self, name: str, agent: Any) -> None:
        """注册 Agent 实例"""
        self._agents[name] = agent
        print(f"[AgentRegistry] Registered agent: {name}")

    def get(self, name: str) -> Optional[Any]:
        """获取 Agent 实例"""
        return self._agents.get(name)

    def list_agents(self) -> list:
        """列出所有已注册的 Agent"""
        return list(self._agents.keys())

    def register_action_handler(self, action: str, handler: Callable) -> None:
        """注册 action 处理器"""
        self._handlers[action] = handler

    def register_request_handler(self, agent_name: str, handler: Callable) -> None:
        """注册 Agent 请求处理器"""
        self._request_handlers[agent_name] = handler

    def call(self, from_agent: str, to_agent: str, action: str,
             payload: Dict[str, Any] = None, timeout: float = 30.0) -> AgentResponse:
        """
        同步调用另一个 Agent

        Args:
            from_agent: 调用方 Agent 名称
            to_agent: 目标 Agent 名称
            action: 操作名称
            payload: 传递给目标 Agent 的参数
            timeout: 超时时间（秒）

        Returns:
            AgentResponse: 响应结果
        """
        request_id = f"{from_agent}_{to_agent}_{action}_{int(time.time() * 1000)}"
        request = AgentRequest(request_id, from_agent, to_agent, action, payload)

        print(f"[AgentRegistry] {from_agent} -> {to_agent}: {action}")

        target_agent = self._agents.get(to_agent)
        if not target_agent:
            return AgentResponse(request_id, False, error=f"Agent not found: {to_agent}")

        try:
            # 根据 action 调用对应的处理方法
            if action == "run_crawl" and hasattr(target_agent, "run_crawl"):
                result = target_agent.run_crawl()
                return AgentResponse(request_id, True, result=result)
            elif action == "generate_summary" and hasattr(target_agent, "generate_summary"):
                team_id = payload.get("team_id") if payload else None
                result = target_agent.generate_summary(team_id)
                return AgentResponse(request_id, True, result=result)
            elif hasattr(target_agent, action):
                method = getattr(target_agent, action)
                result = method(**payload) if payload else method()
                return AgentResponse(request_id, True, result=result)
            else:
                # 尝试调用注册的处理器
                handler = self._request_handlers.get(to_agent)
                if handler:
                    result = handler(request)
                    return AgentResponse(request_id, True, result=result)

                return AgentResponse(
                    request_id, False,
                    error=f"Action not supported: {action} on {to_agent}"
                )
        except Exception as e:
            return AgentResponse(request_id, False, error=str(e))

    def notify(self, from_agent: str, to_agent: str, action: str,
               payload: Dict[str, Any] = None) -> bool:
        """
        异步通知另一个 Agent（不等待响应）

        Args:
            from_agent: 通知方 Agent 名称
            to_agent: 目标 Agent 名称
            action: 操作名称
            payload: 传递的参数

        Returns:
            bool: 是否成功发送
        """
        request_id = f"{from_agent}_{to_agent}_{action}_{int(time.time() * 1000)}"
        request = AgentRequest(request_id, from_agent, to_agent, action, payload)

        print(f"[AgentRegistry] {from_agent} => {to_agent}: {action} (async)")

        target_agent = self._agents.get(to_agent)
        if not target_agent:
            print(f"[AgentRegistry] Warning: Agent not found: {to_agent}")
            return False

        try:
            # 异步执行
            def _run():
                try:
                    if action == "run_crawl" and hasattr(target_agent, "run_crawl"):
                        target_agent.run_crawl()
                    elif hasattr(target_agent, action):
                        method = getattr(target_agent, action)
                        method(**payload) if payload else method()
                except Exception as e:
                    print(f"[AgentRegistry] Async action error: {e}")

            thread = threading.Thread(target=_run, daemon=True)
            thread.start()
            return True
        except Exception as e:
            print(f"[AgentRegistry] Notify error: {e}")
            return False


# 全局实例
registry = AgentRegistry()
