"""Agent 基类 (s01)"""
import os
import json
import time
import random
import logging
import threading
from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional
from anthropic import Anthropic
from src.intelligence import ContextCompactor, CompactConfig
from .circuit_breaker import CircuitBreaker, CircuitBreakerConfig, CircuitBreakerOpenError

# 模块级 logger
_logger = logging.getLogger("base_agent")


def serialize_content_block(block) -> Dict:
    """
    将内容块序列化为可JSON序列化的字典
    支持多种模型返回的块类型:
    - Anthropic: TextBlock, ToolUseBlock, ThinkingBlock
    - MiniMax: ThinkingBlock
    - 其他: 自动检测属性
    """
    result = {"type": getattr(block, "type", "unknown")}

    # 尝试获取文本内容
    if hasattr(block, "text") and block.text:
        result["text"] = block.text
    elif hasattr(block, "thinking") and block.thinking:
        result["text"] = block.thinking
        result["type"] = "thinking"
    elif hasattr(block, "name") and block.name:
        # ToolUseBlock
        result["name"] = block.name
        result["input"] = block.input
        result["id"] = block.id

    return result


def extract_text_from_content(content: List) -> str:
    """从内容块列表中提取纯文本"""
    text_parts = []
    for block in content:
        if hasattr(block, "text") and block.text:
            text_parts.append(block.text)
        elif hasattr(block, "thinking") and block.thinking:
            # MiniMax 的 thinking 模式
            text_parts.append(block.thinking)
    return "".join(text_parts)


class BaseAgent(ABC):
    """Agent 基类 - 实现 s01 的 Agent Loop"""

    def __init__(self, name: str, system_prompt: str = "", compact_config: CompactConfig = None):
        self.name = name
        self.system_prompt = system_prompt or self._default_prompt()

        # 上下文压缩器
        self.compactor = ContextCompactor(compact_config)

        # Circuit Breaker（熔断器）
        cb_config = CircuitBreakerConfig(
            failure_threshold=5,
            success_threshold=2,
            timeout=60.0
        )
        self.circuit_breaker = CircuitBreaker(f"agent_{name}", cb_config)

        # 从配置获取模型
        from src.config import config
        agent_cfg = config.agents.get(name)
        self.model = agent_cfg.model if agent_cfg else "claude-sonnet-4-20250514"
        self.max_tokens = agent_cfg.max_tokens if agent_cfg else 16384

        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY not set")

        # 支持自定义 API 地址（如 MiniMax）
        base_url = os.getenv("ANTHROPIC_BASE_URL", "").strip()
        if base_url:
            self.client = Anthropic(api_key=api_key, base_url=base_url)
        else:
            self.client = Anthropic(api_key=api_key)

        self._channel = None

    def _call_api(self, **kwargs):
        """调用 Anthropic API（使用 streaming + 指数退避重试）"""
        max_retries = 5
        base_delay = 2.0

        for attempt in range(max_retries + 1):
            try:
                with self.client.messages.stream(**kwargs) as stream:
                    return stream.get_final_message()
            except Exception as e:
                if attempt >= max_retries or not self._is_retryable(e):
                    raise
                delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
                _logger.warning(
                    f"[{self.name}] API 调用失败 (尝试 {attempt + 1}/{max_retries + 1}), "
                    f"{delay:.1f}s 后重试: {e}"
                )
                time.sleep(delay)

    @staticmethod
    def _is_retryable(error: Exception) -> bool:
        """判断是否为可重试的瞬态错误（服务器繁忙/限流/超时等）"""
        err_str = str(error).lower()
        retryable_keywords = [
            "overloaded", "rate_limit", "rate limit",
            "server_error", "server error", "internal server error",
            "service unavailable", "bad gateway",
            "busy", "繁忙", "timeout", "timed out",
            "connection", "temporarily",
            "529", "502", "503", "500",
        ]
        return any(kw in err_str for kw in retryable_keywords)

    def set_channel(self, channel):
        """设置消息通道，用于发送回复"""
        self._channel = channel

    def send(self, to: str, text: str, **kwargs) -> bool:
        """发送消息"""
        if self._channel:
            return self._channel.send(to, text, **kwargs)
        # 默认打印到 stdout
        print(f"\nAssistant: {text}\n")
        return True

    @abstractmethod
    def _default_prompt(self) -> str:
        """默认系统提示词"""
        pass

    def _apply_compaction(self, messages: List[Dict]) -> List[Dict]:
        """应用上下文压缩 (Layer 1: Micro + Layer 2: Auto)"""
        # Layer 1: Micro-compact - 每轮都运行
        messages = self.compactor.micro_compact(messages)

        # Layer 2: Auto-compact - 检查是否需要触发
        if self.compactor.should_auto_compact(messages, self.system_prompt):
            messages = self.compactor.auto_compact(messages, self.client, self.model)

        return messages

    def full_compact(self, keep_last: int = 1) -> None:
        """
        手动压缩 (Layer 3: Full-compact)
        用户触发，用于明确的"重新开始"场景
        """
        # 子类可以重写此方法来清空自己的消息历史
        pass

    def get_compact_stats(self) -> Dict[str, Any]:
        """获取压缩统计信息"""
        return self.compactor.get_stats()

    def _validate_messages(self, messages: List[Dict]) -> List[Dict]:
        """Validate and repair message history for API consistency.

        Ensures:
        1. Every tool_result references a tool_use_id that exists in a preceding assistant message
        2. Every tool_use block has a corresponding tool_result in the next user message
        """
        if not messages:
            return messages

        # Step 1: Collect all valid tool_use_ids from assistant messages
        valid_tool_ids = set()
        for msg in messages:
            if msg.get("role") == "assistant" and isinstance(msg.get("content"), list):
                for block in msg["content"]:
                    if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id"):
                        valid_tool_ids.add(block["id"])

        # Step 2: Build repaired message list
        result = []
        i = 0
        while i < len(messages):
            msg = messages[i]

            if msg.get("role") == "assistant" and isinstance(msg.get("content"), list):
                # Check if this assistant message has tool_use blocks
                tool_use_ids_in_msg = []
                for block in msg["content"]:
                    if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id"):
                        tool_use_ids_in_msg.append(block["id"])

                result.append(msg)

                if tool_use_ids_in_msg:
                    # The next message must be a user message with tool_results for ALL tool_use_ids
                    next_msg = messages[i + 1] if i + 1 < len(messages) else None
                    if next_msg and next_msg.get("role") == "user":
                        next_content = next_msg.get("content", [])
                        if isinstance(next_content, list):
                            provided_ids = set()
                            new_content = []
                            for block in next_content:
                                if isinstance(block, dict) and block.get("type") == "tool_result":
                                    tid = block.get("tool_use_id", "")
                                    if tid in valid_tool_ids:
                                        provided_ids.add(tid)
                                        new_content.append(block)
                                    # else: orphaned tool_result, skip it
                                else:
                                    new_content.append(block)

                            # Add dummy tool_results for missing ids
                            for tid in tool_use_ids_in_msg:
                                if tid not in provided_ids:
                                    new_content.append({
                                        "type": "tool_result",
                                        "tool_use_id": tid,
                                        "content": "[工具调用结果因上下文压缩而丢失]"
                                    })

                            result.append({**next_msg, "content": new_content})
                            i += 2
                            continue
                        elif isinstance(next_content, str):
                            # Next message content is a plain string, convert to list
                            new_content = [{"type": "text", "text": next_content}]
                            for tid in tool_use_ids_in_msg:
                                new_content.append({
                                    "type": "tool_result",
                                    "tool_use_id": tid,
                                    "content": "[工具调用结果因上下文压缩而丢失]"
                                })
                            result.append({**next_msg, "content": new_content})
                            i += 2
                            continue
                    else:
                        # No next user message - add a synthetic one with dummy tool_results
                        new_content = []
                        for tid in tool_use_ids_in_msg:
                            new_content.append({
                                "type": "tool_result",
                                "tool_use_id": tid,
                                "content": "[工具调用结果因上下文压缩而丢失]"
                            })
                        result.append({"role": "user", "content": new_content})
                        i += 1
                        continue

            elif msg.get("role") == "user" and isinstance(msg.get("content"), list):
                # Remove orphaned tool_results (not preceded by a matching tool_use)
                new_content = []
                modified = False
                for block in msg["content"]:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        tid = block.get("tool_use_id", "")
                        if tid not in valid_tool_ids:
                            modified = True
                            continue  # Remove orphaned tool_result
                    new_content.append(block)

                if modified:
                    if not new_content:
                        new_content = [{"type": "text", "text": "[上下文已压缩，工具调用记录丢失]"}]
                    result.append({**msg, "content": new_content})
                else:
                    result.append(msg)
            else:
                result.append(msg)

            i += 1

        return result

    def run(self, user_message: str, messages: List[Dict] = None,
            tools: List[Dict] = None, tool_handlers: Dict = None,
            override_system_prompt: str = None) -> tuple[str, List[Dict]]:
        """
        执行 Agent 循环 (s01: while True + stop_reason)
        返回: (最终回复, 更新后的 messages)

        Args:
            user_message: 用户消息
            messages: 可选的历史消息列表
            tools: 可用的工具列表
            tool_handlers: 工具名称到处理函数的映射
            override_system_prompt: 可选的覆盖系统提示词
        """
        # 检查 Circuit Breaker 状态
        if not self.circuit_breaker.can_execute():
            stats = self.circuit_breaker.get_stats()
            raise CircuitBreakerOpenError(
                f"Agent '{self.name}' 的 Circuit Breaker 已打开。"
                f"请稍后再试。状态: {stats}"
            )

        # 使用覆盖的系统提示词或默认的
        system_prompt = override_system_prompt or self.system_prompt

        _logger.debug(f"user_message: {user_message[:50] if user_message else 'None'}...")
        _logger.debug(f"初始 messages 数量: {len(messages) if messages else 0}")

        # 如果传入了历史消息，需要将新问题添加到末尾
        if messages is not None:
            # 已有历史消息，将新问题追加到末尾
            messages = list(messages)  # 复制一份，避免修改原列表
            messages.append({"role": "user", "content": user_message})
            _logger.debug(f"将新问题追加到历史消息后，messages 数量: {len(messages)}")
        else:
            messages = [{"role": "user", "content": user_message}]
            _logger.debug(f"创建了新的初始消息")

        # 应用上下文压缩 (Layer 1 & 2)
        messages = self._apply_compaction(messages)
        _logger.debug(f"压缩后 messages 数量: {len(messages)}")

        # 验证消息历史一致性（修复压缩导致的 tool_use/tool_result 不匹配）
        messages = self._validate_messages(messages)

        iteration = 0
        max_iterations = 50

        while iteration < max_iterations:
            iteration += 1

            try:
                # 使用 CircuitBreaker 包装 API 调用（streaming 避免超时）
                response = self.circuit_breaker.execute(
                    self._call_api,
                    model=self.model,
                    max_tokens=self.max_tokens,
                    system=system_prompt,
                    tools=tools or [],
                    messages=messages,
                )
            except CircuitBreakerOpenError:
                raise
            except Exception:
                raise

            # 序列化内容块（支持多种模型）
            serialized_content = [serialize_content_block(block) for block in response.content]
            messages.append({"role": "assistant", "content": serialized_content})

            if response.stop_reason == "end_turn":
                # 使用辅助函数提取文本
                text = extract_text_from_content(response.content)
                return text, messages

            elif response.stop_reason == "tool_use":
                tool_results = []
                called_tools = []
                for block in response.content:
                    if block.type != "tool_use":
                        continue

                    tool_name = block.name
                    tool_input = block.input
                    called_tools.append(tool_name)

                    if tool_handlers and tool_name in tool_handlers:
                        try:
                            result = tool_handlers[tool_name](**tool_input)
                        except Exception as e:
                            result = f"工具执行失败: {e}"
                    else:
                        result = f"Unknown tool: {tool_name}"

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })

                _logger.info(
                    f"[{self.name}] iteration={iteration}, "
                    f"tools={called_tools}"
                )

                messages.append({"role": "user", "content": tool_results})

                # 在工具调用后再次应用压缩
                messages = self._apply_compaction(messages)

                # 迭代干预：接近上限时注入收尾指令，阻止模型继续循环
                wrap_up_threshold = max(3, int(max_iterations * 0.7))
                if iteration >= wrap_up_threshold:
                    remaining = max_iterations - iteration
                    _logger.warning(
                        f"[{self.name}] iteration={iteration} >= {wrap_up_threshold}, "
                        f"injecting wrap-up (remaining={remaining})"
                    )
                    messages.append({
                        "role": "user",
                        "content": (
                            f"[系统提示] 你已经调用了 {iteration} 次工具，"
                            f"还剩 {remaining} 次机会。请立即根据已获取的信息给出最终回答，"
                            f"不要再调用任何工具。如果信息不足，直接说明缺少什么。"
                        ),
                    })

            else:
                # 其他 stop_reason，使用辅助函数提取文本
                text = extract_text_from_content(response.content)
                return text, messages

        # 超过迭代上限：优雅降级，提取已有文本或返回提示
        _logger.warning(
            f"[{self.name}] Agent loop exceeded {max_iterations} iterations, "
            f"gracefully degrading"
        )
        # 尝试从最后一条 assistant 消息中提取文本
        last_text = ""
        for msg in reversed(messages):
            if msg.get("role") == "assistant":
                content = msg.get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("text"):
                            last_text = block["text"]
                            break
                elif isinstance(content, str):
                    last_text = content
                    break
                if last_text:
                    break

        if last_text:
            return last_text, messages
        return f"抱歉，处理过程中工具调用次数过多（{max_iterations}次），请尝试简化问题或稍后重试。", messages

    def get_circuit_breaker_status(self) -> dict:
        """获取 Circuit Breaker 状态"""
        return self.circuit_breaker.get_stats()

    def reset_circuit_breaker(self) -> None:
        """手动重置 Circuit Breaker"""
        self.circuit_breaker.reset()
