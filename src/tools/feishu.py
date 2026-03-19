"""飞书发送工具 (s04)"""
import os
import json
import httpx
import time
from typing import Optional

class FeishuSender:
    def __init__(self):
        self.app_id = os.getenv("FEISHU_APP_ID")
        self.app_secret = os.getenv("FEISHU_APP_SECRET")
        self.api_base = "https://open.feishu.cn/open-apis"
        self._token = ""
        self._token_expires_at = 0.0

    def _refresh_token(self) -> bool:
        """刷新 tenant_access_token"""
        if not self.app_id or not self.app_secret:
            return False

        if self._token and time.time() < self._token_expires_at:
            return True

        try:
            with httpx.Client(timeout=15.0) as client:
                resp = client.post(
                    f"{self.api_base}/auth/v3/tenant_access_token/internal",
                    json={"app_id": self.app_id, "app_secret": self.app_secret}
                )
                data = resp.json()
                if data.get("code") != 0:
                    print(f"Token error: {data.get('msg')}")
                    return False
                self._token = data.get("tenant_access_token", "")
                self._token_expires_at = time.time() + data.get("expire", 7200) - 300
                return True
        except Exception as e:
            print(f"Token refresh failed: {e}")
            return False

    def send(self, chat_id: str, text: str) -> bool:
        """发送消息到群"""
        if not self._refresh_token():
            return False

        try:
            with httpx.Client(timeout=15.0) as client:
                resp = client.post(
                    f"{self.api_base}/im/v1/messages",
                    params={"receive_id_type": "chat_id"},
                    headers={"Authorization": f"Bearer {self._token}"},
                    json={
                        "receive_id": chat_id,
                        "msg_type": "text",
                        "content": json.dumps({"text": text})
                    }
                )
                data = resp.json()
                if data.get("code") != 0:
                    print(f"Send error: {data.get('msg')}")
                    return False
                return True
        except Exception as e:
            print(f"Feishu send failed: {e}")
            return False

def tool_feishu_send(chat_id: str, text: str) -> str:
    """发送飞书消息"""
    sender = FeishuSender()
    ok = sender.send(chat_id, text)
    return "飞书消息发送成功" if ok else "飞书消息发送失败"

FEISHU_TOOLS = [
    {
        "name": "feishu_send",
        "description": "发送飞书消息",
        "input_schema": {
            "type": "object",
            "properties": {
                "chat_id": {"type": "string", "description": "飞书群 ID"},
                "text": {"type": "string", "description": "消息内容"}
            },
            "required": ["chat_id", "text"]
        }
    }
]

FEISHU_HANDLERS = {
    "feishu_send": tool_feishu_send,
}
