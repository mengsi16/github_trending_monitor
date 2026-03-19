"""网关路由逻辑 (s05)"""
from ..channels import InboundMessage
from .binding import BindingTable, Binding
from src.config import config

def create_default_bindings() -> BindingTable:
    """创建默认路由表"""
    bt = BindingTable()

    # 默认路由到 qa agent
    bt.add_default("qa")

    return bt

class Gateway:
    """网关 - 根据消息路由到对应 Agent"""

    def __init__(self):
        self.bindings = create_default_bindings()
        self.agent_factory = {}

    def register_agent(self, name: str, agent):
        """注册 Agent"""
        self.agent_factory[name] = agent

    def route(self, message: InboundMessage):
        """路由消息到 Agent"""
        agent_id, binding = self.bindings.resolve(
            channel=message.channel,
            account_id=message.account_id,
            peer_id=message.peer_id,
        )

        if not agent_id:
            return None, "No agent found for this message"

        agent = self.agent_factory.get(agent_id)
        if not agent:
            return None, f"Agent {agent_id} not registered"

        return agent, None

    def add_team_binding(self, team_id: str, channel: str, peer_id: str):
        """添加团队特定路由 (s05 Tier 1)"""
        self.bindings.add(Binding(
            agent_id="summarizer",
            tier=1,
            match_key="peer_id",
            match_value=f"{channel}:{peer_id}",
        ))
