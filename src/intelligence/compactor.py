"""上下文压缩模块 (s03) - 三层压缩策略"""
import os
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field
from anthropic import Anthropic

# tiktoken 是可选依赖
try:
    import tiktoken
    _HAS_TIKTOKEN = True
except ImportError:
    _HAS_TIKTOKEN = False


@dataclass
class CompactConfig:
    """压缩配置"""
    # Micro-compact: 截断阈值
    tool_result_max_chars: int = 4000  # tool_result 超过此长度则截断
    tool_result_keep_suffix: str = "\n...[已截断]"

    # Auto-compact: token 阈值
    auto_compact_threshold: int = 80000  # 超过此 token 数触发自动压缩
    auto_compact_ratio: float = 0.5  # 压缩掉的消息比例

    # 保留最近 N 轮消息不被压缩
    keep_recent_rounds: int = 2


class ContextCompactor:
    """三层上下文压缩器"""

    def __init__(self, config: CompactConfig = None):
        self.config = config or CompactConfig()
        self._encoding = None
        self._compact_count = 0  # 压缩次数统计

    def _get_encoding(self):
        """获取 tiktoken 编码器 (lazy init)"""
        if self._encoding is None:
            if _HAS_TIKTOKEN:
                try:
                    self._encoding = tiktoken.get_encoding("claude100k")
                except Exception:
                    # 如果获取失败，使用估算
                    self._encoding = False  # 标记为不可用
            else:
                self._encoding = False  # 标记为不可用
        return self._encoding if self._encoding else None

    def count_tokens(self, messages: List[Dict], system: str = "") -> int:
        """计算消息的 token 数量"""
        # 估算 system prompt
        system_tokens = len(system) // 4

        encoding = self._get_encoding()
        if encoding:
            # 使用 tiktoken 精确计算
            text = self._serialize_messages(messages)
            return len(encoding.encode(text)) + system_tokens
        else:
            # 粗略估算: 1 token ≈ 4 字符 (Claude 的估算)
            text = self._serialize_messages(messages)
            return (len(text) + system_tokens) // 4

    def _serialize_messages(self, messages: List[Dict]) -> str:
        """将消息序列化为文本"""
        lines = []
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")

            if isinstance(content, list):
                parts = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            parts.append(block.get("text", ""))
                        elif block.get("type") == "tool_result":
                            # tool_result 只计算前 N 个字符
                            result = block.get("content", "")
                            parts.append(result[:self.config.tool_result_max_chars])
                    elif isinstance(block, str):
                        parts.append(block)
                content = " ".join(parts)
            elif isinstance(content, str):
                pass
            else:
                content = str(content)

            lines.append(f"{role}: {content}")
        return "\n".join(lines)

    # ========== Layer 1: Micro-compact (微压缩) ==========

    def micro_compact(self, messages: List[Dict]) -> List[Dict]:
        """
        微压缩 - 每轮都运行，几乎零成本
        截断旧消息中的 tool_result 块，去除不再需要的冗长命令输出
        """
        if not messages:
            return messages

        result = []
        for i, msg in enumerate(messages):
            if msg.get("role") == "user" and isinstance(msg.get("content"), list):
                # 检查是否是较旧的消息（保留最新的几轮）
                is_recent = i >= len(messages) - (self.config.keep_recent_rounds * 2)

                new_content = []
                for block in msg["content"]:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        content = block.get("content", "")
                        # 只对旧消息进行截断
                        if not is_recent and len(content) > self.config.tool_result_max_chars:
                            content = (
                                content[:self.config.tool_result_max_chars]
                                + self.config.tool_result_keep_suffix
                            )
                        block = dict(block)
                        block["content"] = content
                    new_content.append(block)
                result.append({**msg, "content": new_content})
            else:
                result.append(msg)

        return result

    # ========== Layer 2: Auto-compact (自动压缩) ==========

    def should_auto_compact(self, messages: List[Dict], system: str = "") -> bool:
        """检查是否需要触发自动压缩"""
        token_count = self.count_tokens(messages, system)
        return token_count > self.config.auto_compact_threshold

    def auto_compact(self, messages: List[Dict], client: Anthropic = None,
                     model: str = "claude-sonnet-4-20250514") -> List[Dict]:
        """
        自动压缩 - token 数超过阈值时触发
        调用 LLM 生成对话摘要，代价高但能大幅缩减上下文
        """
        if not messages:
            return messages

        # 计算要压缩的消息数量
        total_messages = len(messages)
        keep_count = max(4, int(total_messages * (1 - self.config.auto_compact_ratio)))
        keep_count = max(keep_count, self.config.keep_recent_rounds * 2)

        # 保留最近的消息
        recent_messages = messages[keep_count:]
        old_messages = messages[:keep_count]

        if not old_messages:
            return messages

        # 用 LLM 生成摘要
        summary = self._generate_summary(old_messages, client, model)

        # 构建压缩后的消息
        compacted = [
            {
                "role": "user",
                "content": f"【之前对话的摘要】\n{summary}\n【摘要结束】"
            },
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "了解，我会参考之前的对话背景。"}]
            }
        ]
        compacted.extend(recent_messages)

        self._compact_count += 1
        print(f"[ContextCompactor] Auto-compact #{self._compact_count}: "
              f"{len(old_messages)} messages -> 1 summary, "
              f"kept {len(recent_messages)} recent messages")

        return compacted

    def _generate_summary(self, messages: List[Dict], client: Anthropic,
                          model: str) -> str:
        """调用 LLM 生成消息摘要"""
        if client is None:
            api_key = os.getenv("ANTHROPIC_API_KEY")
            if not api_key:
                return "[无法生成摘要: 缺少 API Key]"
            client = Anthropic(api_key=api_key)

        # 序列化旧消息
        old_text = self._serialize_for_summary(messages)

        summary_prompt = f"""请简洁总结以下对话的要点，保留关键信息和结论：

{old_text}

要求：
- 不超过 500 字
- 保留关键的技术细节和数据
- 使用 bullet point 格式
"""

        try:
            response = client.messages.create(
                model=model,
                max_tokens=1024,
                system="你是一个对话摘要生成器。请简洁准确地提取关键信息。",
                messages=[{"role": "user", "content": summary_prompt}]
            )
            return response.content[0].text if response.content else ""
        except Exception as e:
            return f"[摘要生成失败: {e}]"

    def _serialize_for_summary(self, messages: List[Dict]) -> str:
        """将消息序列化为摘要提示"""
        lines = []
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")

            if isinstance(content, list):
                parts = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            parts.append(block.get("text", ""))
                        elif block.get("type") == "tool_result":
                            result = block.get("content", "")
                            # tool_result 只取前面一部分
                            parts.append(f"[工具返回: {result[:500]}...]")
                content = " | ".join(parts)
            elif isinstance(content, str):
                content = content[:1000]  # 限制单条消息长度

            lines.append(f"{role}: {content[:500]}")
        return "\n".join(lines)

    # ========== Layer 3: Full-compact (手动压缩) ==========

    def full_compact(self, messages: List[Dict], keep_last: int = 1) -> List[Dict]:
        """
        手动压缩 - 用户触发，用于明确的"重新开始"场景
        保留最近 N 轮完整对话，其余全部丢弃
        """
        if not messages:
            return messages

        # 找到最近的 user-assistant 对
        result = []
        user_assist_pairs = []

        current_user = None
        current_assistant = None

        for msg in messages:
            if msg.get("role") == "user":
                current_user = msg
                current_assistant = None
            elif msg.get("role") == "assistant" and current_user:
                current_assistant = msg
                user_assist_pairs.append((current_user, current_assistant))
                current_user = None
                current_assistant = None

        # 保留最近 keep_last 轮
        keep_pairs = user_assist_pairs[-keep_last:] if user_assist_pairs else []

        for user_msg, assistant_msg in keep_pairs:
            result.append(user_msg)
            if assistant_msg:
                result.append(assistant_msg)

        self._compact_count += 1
        original_count = len(messages)
        print(f"[ContextCompactor] Full-compact #{self._compact_count}: "
              f"{original_count} messages -> {len(result)} messages "
              f"(kept last {keep_last} rounds)")

        return result

    def get_stats(self) -> Dict[str, Any]:
        """获取压缩统计信息"""
        return {
            "total_compact_count": self._compact_count,
            "config": {
                "tool_result_max_chars": self.config.tool_result_max_chars,
                "auto_compact_threshold": self.config.auto_compact_threshold,
                "keep_recent_rounds": self.config.keep_recent_rounds,
            }
        }


# 全局默认实例
default_compactor = ContextCompactor()
