"""CLI 通道 (s04)"""
import sys
from .base import Channel, InboundMessage

class CLIChannel(Channel):
    name = "cli"

    def __init__(self):
        self.account_id = "cli-local"

    def receive(self) -> InboundMessage:
        try:
            text = input("\nYou > ").strip()
            if not text:
                return InboundMessage(
                    text="",
                    sender_id="cli-user",
                    channel="cli",
                    account_id=self.account_id,
                    peer_id="cli-user",
                )
            return InboundMessage(
                text=text,
                sender_id="cli-user",
                channel="cli",
                account_id=self.account_id,
                peer_id="cli-user",
            )
        except (KeyboardInterrupt, EOFError):
            return None

    def send(self, to: str, text: str, **kwargs) -> bool:
        print(f"\nAssistant: {text}\n")
        return True
