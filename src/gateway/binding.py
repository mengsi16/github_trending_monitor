"""BindingTable 路由 (s05)"""
from dataclasses import dataclass
from typing import Optional, Tuple

@dataclass
class Binding:
    """路由绑定"""
    agent_id: str
    tier: int           # 1-5, 越小越具体
    match_key: str      # peer_id, account_id, channel, default
    match_value: str    # 具体值
    priority: int = 0

class BindingTable:
    """5层路由表 (s05)"""

    def __init__(self):
        self._bindings: list[Binding] = []

    def add(self, binding: Binding):
        self._bindings.append(binding)
        # 按 tier 排序
        self._bindings.sort(key=lambda b: (b.tier, -b.priority))

    def resolve(self, channel: str = "", account_id: str = "",
                guild_id: str = "", peer_id: str = "") -> Tuple[Optional[str], Optional[Binding]]:
        """
        解析路由
        返回: (agent_id, binding)
        """
        for b in self._bindings:
            if b.tier == 1 and b.match_key == "peer_id":
                if b.match_value == peer_id or b.match_value == f"{channel}:{peer_id}":
                    return b.agent_id, b

            elif b.tier == 2 and b.match_key == "guild_id":
                if b.match_value == guild_id:
                    return b.agent_id, b

            elif b.tier == 3 and b.match_key == "account_id":
                if b.match_value == account_id:
                    return b.agent_id, b

            elif b.tier == 4 and b.match_key == "channel":
                if b.match_value == channel:
                    return b.agent_id, b

            elif b.tier == 5 and b.match_key == "default":
                return b.agent_id, b

        return None, None

    def add_default(self, agent_id: str):
        """添加默认路由"""
        self.add(Binding(agent_id=agent_id, tier=5, match_key="default", match_value="*"))
