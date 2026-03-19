"""Channel 基类 (s04)"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

@dataclass
class InboundMessage:
    """统一的消息格式 (s04)"""
    text: str
    sender_id: str
    channel: str = ""
    account_id: str = ""
    peer_id: str = ""
    is_group: bool = False

class Channel(ABC):
    """Channel 抽象基类"""
    name: str = "unknown"

    @abstractmethod
    def receive(self) -> Optional[InboundMessage]:
        """接收消息"""
        pass

    @abstractmethod
    def send(self, to: str, text: str, **kwargs) -> bool:
        """发送消息"""
        pass

    def close(self):
        """关闭通道"""
        pass
