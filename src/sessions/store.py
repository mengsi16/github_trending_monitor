import json
import time
from pathlib import Path
from typing import List, Dict, Any

WORKSPACE_DIR = Path("./workspace")
SESSIONS_DIR = WORKSPACE_DIR / ".sessions"

class SessionStore:
    """JSONL 持久化会话存储 (s03)"""

    def __init__(self, agent_id: str = "main"):
        self.agent_id = agent_id
        self.session_dir = SESSIONS_DIR / "agents" / agent_id / "sessions"
        self.session_dir.mkdir(parents=True, exist_ok=True)

    def _session_path(self, session_id: str) -> Path:
        return self.session_dir / f"{session_id}.jsonl"

    def save_turn(self, session_id: str, role: str, content: Any) -> None:
        """追加写入一条记录"""
        path = self._session_path(session_id)
        record = {"type": role, "content": content, "ts": time.time()}
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def load_session(self, session_id: str) -> List[Dict]:
        """从 JSONL 重放会话"""
        path = self._session_path(session_id)
        if not path.exists():
            return []

        messages = []
        for line in path.read_text(encoding="utf-8").strip().split("\n"):
            if not line:
                continue
            record = json.loads(line)
            rtype = record.get("type")

            if rtype == "user":
                messages.append({"role": "user", "content": record["content"]})
            elif rtype == "assistant":
                content = record["content"]
                if isinstance(content, str):
                    content = [{"type": "text", "text": content}]
                messages.append({"role": "assistant", "content": content})
            elif rtype == "tool_use":
                block = {"type": "tool_use", "id": record["tool_use_id"],
                         "name": record["name"], "input": record["input"]}
                if messages and messages[-1]["role"] == "assistant":
                    messages[-1]["content"].append(block)
                else:
                    messages.append({"role": "assistant", "content": [block]})
            elif rtype == "tool_result":
                result_block = {"type": "tool_result",
                                "tool_use_id": record["tool_use_id"],
                                "content": record["content"]}
                if (messages and messages[-1]["role"] == "user"
                        and isinstance(messages[-1]["content"], list)
                        and messages[-1]["content"][0].get("type") == "tool_result"):
                    messages[-1]["content"].append(result_block)
                else:
                    messages.append({"role": "user", "content": [result_block]})

        return messages

    def list_sessions(self) -> List[str]:
        """列出所有会话"""
        return [p.stem for p in self.session_dir.glob("*.jsonl")]
