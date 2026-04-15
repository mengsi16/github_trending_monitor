from typing import List, Dict, Any
import os
import time
import random
import logging

_logger = logging.getLogger("context_guard")


def _is_retryable(error: Exception) -> bool:
    err_str = str(error).lower()
    return any(kw in err_str for kw in [
        "overloaded", "rate_limit", "rate limit", "busy", "繁忙",
        "server_error", "server error", "service unavailable",
        "timeout", "timed out", "connection", "529", "502", "503", "500",
    ])


class ContextGuard:
    """3阶段上下文溢出保护 (s03)"""

    def __init__(self, api_client=None, model: str = "claude-sonnet-4-20250514", max_retries: int = 2):
        self.client = api_client
        self.model = model
        self.max_retries = max_retries

    def guard_api_call(self, client, messages: List[Dict], system: str = "",
                       tools: List[Dict] = None, max_tokens: int = 8096) -> Any:
        """包裹 API 调用，溢出时自动重试"""
        from anthropic import Anthropic

        # 如果没有传入 client，尝试创建
        if client is None:
            api_key = os.getenv("ANTHROPIC_API_KEY")
            if not api_key:
                raise ValueError("ANTHROPIC_API_KEY not set")
            client = Anthropic(api_key=api_key)

        current_messages = list(messages)

        for attempt in range(self.max_retries + 1):
            # 每次尝试内部带指数退避重试（应对服务器繁忙）
            api_retries = 3
            base_delay = 2.0
            last_exc = None

            for api_attempt in range(api_retries + 1):
                try:
                    kwargs = dict(
                        model=self.model,
                        max_tokens=max_tokens,
                        system=system,
                        messages=current_messages,
                    )
                    if tools:
                        kwargs["tools"] = tools
                    with client.messages.stream(**kwargs) as stream:
                        result = stream.get_final_message()
                    # 成功，更新原始 messages
                    if current_messages is not messages:
                        messages.clear()
                        messages.extend(current_messages)
                    return result

                except Exception as exc:
                    last_exc = exc
                    error_str = str(exc).lower()
                    is_overflow = ("context" in error_str or "token" in error_str
                                  or "maximum" in error_str)

                    if is_overflow:
                        break  # 溢出错误走外层重试（截断/压缩）

                    if api_attempt >= api_retries or not _is_retryable(exc):
                        raise
                    delay = base_delay * (2 ** api_attempt) + random.uniform(0, 1)
                    _logger.warning(
                        f"[ContextGuard] API 调用失败 (尝试 {api_attempt + 1}/{api_retries + 1}), "
                        f"{delay:.1f}s 后重试: {exc}"
                    )
                    time.sleep(delay)
            else:
                # api_retries 耗尽但未 break，不会走到这里
                continue

            # 从内层 break 出来 = 溢出错误，走原有的截断/压缩策略
            if attempt >= self.max_retries:
                raise last_exc

            if attempt == 0:
                current_messages = self._truncate_large_tool_results(current_messages)
            elif attempt == 1:
                current_messages = self._compact_history(client, current_messages)

    def _truncate_large_tool_results(self, messages: List[Dict]) -> List[Dict]:
        """截断超过 8000 字符的工具结果"""
        result = []
        for msg in messages:
            if msg.get("role") == "user" and isinstance(msg.get("content"), list):
                new_content = []
                for block in msg["content"]:
                    if block.get("type") == "tool_result":
                        content = block.get("content", "")
                        if len(content) > 8000:
                            content = content[:8000] + "\n...[truncated]"
                        block = dict(block)
                        block["content"] = content
                    new_content.append(block)
                result.append({**msg, "content": new_content})
            else:
                result.append(msg)
        return result

    def _compact_history(self, client, messages: List[Dict]) -> List[Dict]:
        """用 LLM 压缩历史消息"""
        from anthropic import Anthropic

        keep_count = max(4, int(len(messages) * 0.2))
        compress_count = max(2, int(len(messages) * 0.5))
        compress_count = min(compress_count, len(messages) - keep_count)

        if compress_count <= 0:
            return messages

        old_text = self._serialize_for_summary(messages[:compress_count])
        summary_prompt = f"""请简洁总结以下对话要点，保留关键信息：
{old_text}"""

        max_retries = 3
        base_delay = 2.0
        summary = ""

        for attempt in range(max_retries + 1):
            try:
                with client.messages.stream(
                    model=self.model,
                    max_tokens=2048,
                    system="你是对话摘要生成器。请简洁准确地总结。",
                    messages=[{"role": "user", "content": summary_prompt}]
                ) as stream:
                    summary_resp = stream.get_final_message()
                summary = summary_resp.content[0].text if summary_resp.content else ""
                break
            except Exception as e:
                if attempt >= max_retries or not _is_retryable(e):
                    _logger.warning(f"[ContextGuard] 摘要生成失败: {e}")
                    summary = "[摘要生成失败]"
                    break
                delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
                _logger.warning(
                    f"[ContextGuard] 摘要生成失败 (尝试 {attempt + 1}/{max_retries + 1}), "
                    f"{delay:.1f}s 后重试: {e}"
                )
                time.sleep(delay)

        compacted = [
            {"role": "user", "content": f"[对话摘要]\n{summary}"},
            {"role": "assistant", "content": [{"type": "text", "text": "了解，我会参考之前的对话。"}]}
        ]
        compacted.extend(messages[compress_count:])
        return compacted

    def _serialize_for_summary(self, messages: List[Dict]) -> str:
        lines = []
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if isinstance(content, list):
                parts = []
                for block in content:
                    if block.get("type") == "text":
                        parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_result":
                        parts.append(f"[工具返回]")
                content = " ".join(parts)
            lines.append(f"{role}: {content[:200]}")
        return "\n".join(lines)
