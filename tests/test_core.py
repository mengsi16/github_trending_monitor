"""测试 GitHub Trending Monitor 核心模块"""
import os
import sys
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest


class TestConfig:
    """测试配置模块"""

    def test_config_loads_from_yaml(self, tmp_path):
        """测试配置可以从 YAML 加载"""
        from src.config import Config, TeamConfig, AgentConfig
        import yaml

        # Create temp config.yaml with explicit UTF-8 encoding
        config_content = """
github:
  api_url: https://api.github.com
  top_n: 10

agents:
  qa:
    model: claude-sonnet-4-20250514
    max_tokens: 4096

teams:
  - id: tech
    name: Tech Team
    channels: [email]
    email: test@example.com
"""
        config_file = tmp_path / "config.yaml"
        config_file.write_bytes(config_content.encode('utf-8'))

        with open(config_file, encoding="utf-8") as f:
            data = yaml.safe_load(f)

        teams = [TeamConfig(**t) for t in data.get("teams", [])]
        assert len(teams) == 1
        assert teams[0].id == "tech"
        assert teams[0].name == "Tech Team"

    def test_empty_yaml_falls_back_to_defaults(self):
        """测试空 YAML 内容不会导致崩溃"""
        with patch("src.config.yaml.safe_load", return_value=None):
            from src.config import load_config
            cfg = load_config()
            assert cfg.github_top_n == 20
            assert isinstance(cfg.teams, list)


class TestCircuitBreaker:
    """测试熔断器"""

    def test_circuit_breaker_starts_closed(self):
        """测试熔断器初始状态为关闭"""
        from src.agents.circuit_breaker import CircuitBreaker, CircuitBreakerConfig, CircuitState

        config = CircuitBreakerConfig(failure_threshold=3, timeout=1.0)
        cb = CircuitBreaker("test", config)

        assert cb.state == CircuitState.CLOSED
        assert cb.can_execute() is True

    def test_circuit_breaker_opens_after_failures(self):
        """测试连续失败后熔断器打开"""
        from src.agents.circuit_breaker import CircuitBreaker, CircuitBreakerConfig, CircuitState

        config = CircuitBreakerConfig(failure_threshold=3, timeout=10.0)
        cb = CircuitBreaker("test", config)

        # 模拟3次失败
        for i in range(3):
            cb.record_failure(f"error_{i}")

        assert cb.state == CircuitState.OPEN
        assert cb.can_execute() is False

    def test_circuit_breaker_executes_function(self):
        """测试熔断器可以执行函数"""
        from src.agents.circuit_breaker import CircuitBreaker, CircuitBreakerConfig

        config = CircuitBreakerConfig(failure_threshold=3, timeout=10.0)
        cb = CircuitBreaker("test", config)

        def test_func():
            return "success"

        result = cb.execute(test_func)
        assert result == "success"
        assert cb.get_stats()["successful_calls"] == 1

    def test_circuit_breaker_records_failure(self):
        """测试熔断器记录失败"""
        from src.agents.circuit_breaker import CircuitBreaker, CircuitBreakerConfig

        config = CircuitBreakerConfig(failure_threshold=3, timeout=10.0)
        cb = CircuitBreaker("test", config)

        def failing_func():
            raise ValueError("test error")

        with pytest.raises(ValueError):
            cb.execute(failing_func)

        stats = cb.get_stats()
        assert stats["failed_calls"] == 1
        assert stats["consecutive_failures"] == 1


class TestContextCompactor:
    """测试上下文压缩器"""

    def test_micro_compact_truncates_long_tool_results(self):
        """测试微压缩截断过长的工具结果"""
        from src.intelligence.compactor import ContextCompactor, CompactConfig

        # keep_recent_rounds=1 means keep 1 round (2 messages) from end
        # So with 6 messages, indices 0-3 are "old" and 4-5 are "recent"
        config = CompactConfig(tool_result_max_chars=100, keep_recent_rounds=1)
        compactor = ContextCompactor(config)

        messages = [
            {"role": "user", "content": "问题1"},
            {"role": "assistant", "content": "回答1"},
            {"role": "user", "content": "问题2"},
            {"role": "assistant", "content": "回答2"},
            {"role": "user", "content": "问题3"},  # index 4 - recent, not truncated
            {"role": "user", "content": [  # index 5 - recent
                {"type": "tool_result", "tool_use_id": "1", "content": "x" * 200}
            ]},
        ]

        result = compactor.micro_compact(messages)

        # Old tool_result (index 1, which is in the first 4 messages) should be truncated
        # But actually, micro_compact only looks at role=="user" messages for truncation
        # The old user message at index 0 with content "问题1" won't be truncated because it's not a tool_result
        # Let's check the user message at index 2 which has a tool_result-like content
        # Actually the issue is we need an OLD user message with tool_result content
        messages = [
            {"role": "user", "content": "问题1"},
            {"role": "assistant", "content": "回答1"},
            {"role": "user", "content": [  # index 2 - OLD, should be truncated
                {"type": "tool_result", "tool_use_id": "1", "content": "x" * 200}
            ]},
            {"role": "assistant", "content": "回答2"},
            {"role": "user", "content": "问题3"},  # index 4 - recent
            {"role": "assistant", "content": "回答3"},  # index 5 - recent
        ]

        result = compactor.micro_compact(messages)

        # The tool_result at index 2 should be truncated (it's not recent)
        tool_result_content = result[2]["content"][0]["content"]
        assert len(tool_result_content) <= 100 + len(config.tool_result_keep_suffix)
        assert config.tool_result_keep_suffix in tool_result_content

    def test_should_auto_compact_respects_threshold(self):
        """测试自动压缩检查阈值"""
        from src.intelligence.compactor import ContextCompactor, CompactConfig

        config = CompactConfig(auto_compact_threshold=100)  # 很低的阈值
        compactor = ContextCompactor(config)

        # 创建一个会超过阈值的长消息
        messages = [
            {"role": "user", "content": "x" * 500}
        ]

        assert compactor.should_auto_compact(messages) is True

        # 短消息不应该触发
        short_messages = [{"role": "user", "content": "hi"}]
        assert compactor.should_auto_compact(short_messages) is False

    def test_full_compact_keeps_recent_messages(self):
        """测试完全压缩保留最近的对话"""
        from src.intelligence.compactor import ContextCompactor, CompactConfig

        config = CompactConfig()
        compactor = ContextCompactor(config)

        messages = [
            {"role": "user", "content": "第1轮问题"},
            {"role": "assistant", "content": "第1轮回答"},
            {"role": "user", "content": "第2轮问题"},
            {"role": "assistant", "content": "第2轮回答"},
            {"role": "user", "content": "第3轮问题"},
            {"role": "assistant", "content": "第3轮回答"},
        ]

        result = compactor.full_compact(messages, keep_last=1)

        # 应该只保留最后一轮
        assert len(result) == 2
        assert result[0]["content"] == "第3轮问题"
        assert result[1]["content"] == "第3轮回答"


class TestSQLiteSessionStore:
    """测试 SQLite 会话存储"""

    def test_create_and_load_session(self, tmp_path):
        """测试创建和加载会话"""
        from src.sessions.sqlite_store import SQLiteSessionStore

        # 使用临时目录
        store = SQLiteSessionStore(agent_id="test")

        session_id = store.create_session(title="测试会话")
        assert session_id is not None
        assert len(session_id) == 8

        # 加载会话
        messages = store.load_session(session_id)
        assert messages == []

    def test_save_and_load_message(self, tmp_path):
        """测试保存和加载消息"""
        from src.sessions.sqlite_store import SQLiteSessionStore

        store = SQLiteSessionStore(agent_id="test")

        session_id = store.create_session()

        # 保存消息
        store.save_message(session_id, "user", "你好")
        store.save_message(session_id, "assistant", "你好！有什么可以帮助你的吗？")

        # 加载消息
        messages = store.load_session(session_id)
        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "你好"

    def test_list_sessions(self, tmp_path):
        """测试列出会话"""
        import uuid
        from src.sessions.sqlite_store import SQLiteSessionStore

        # Use unique agent_id to avoid conflicts with other tests
        agent_id = f"test_list_{uuid.uuid4().hex[:8]}"
        store = SQLiteSessionStore(agent_id=agent_id)

        # 创建多个会话
        store.create_session("Session1")
        store.create_session("Session2")

        sessions = store.list_sessions(limit=10)
        # Only count sessions created by this test
        assert len(sessions) >= 2


class TestDeliveryRunner:
    """测试投递 runner"""

    def test_email_sender_called_with_kwargs_subject(self):
        """测试邮件发送使用 subject 关键字参数而非位置参数"""
        from src.delivery.runner import DeliveryRunner

        class FakeQueue:
            def __init__(self):
                self.acked = []
                self.failed = []

            def load_pending(self):
                return [
                    type("Entry", (), {
                        "id": "abc123",
                        "channel": "email",
                        "to": "u@example.com",
                        "subject": "subj",
                        "text": "body",
                    })
                ]

            def ack(self, delivery_id):
                self.acked.append(delivery_id)

            def fail(self, delivery_id, error, backoff, max_retries=5):
                self.failed.append((delivery_id, error, backoff, max_retries))

        runner = DeliveryRunner()
        fake_queue = FakeQueue()
        runner.queue = fake_queue

        called = {}

        def fake_email_sender(to, text, **kwargs):
            called["to"] = to
            called["text"] = text
            called["kwargs"] = kwargs
            runner._stop_event.set()
            return True

        runner.register_sender("email", fake_email_sender)
        runner._loop()

        assert fake_queue.acked == ["abc123"]
        assert fake_queue.failed == []
        assert called["to"] == "u@example.com"
        assert called["text"] == "body"
        assert called["kwargs"].get("subject") == "subj"


class TestQAAgent:
    """测试 QAAgent 会话行为"""

    def test_load_empty_session_returns_true(self):
        """测试空会话也可成功加载"""
        from src.agents.qa import QAAgent
        from src.sessions.sqlite_store import SQLiteSessionStore

        store = SQLiteSessionStore(agent_id="test_qa_empty_session")
        session_id = store.create_session("empty")

        qa = QAAgent(session_store=store)
        ok = qa.load_session(session_id)

        assert ok is True
        assert qa.get_current_session_id() == session_id


class TestLaneQueue:
    """测试 Lane 队列"""

    def test_lane_queue_single_task(self):
        """测试 Lane 队列执行单个任务"""
        from src.concurrency.lane import LaneQueue

        lane = LaneQueue("test", max_concurrency=1)
        result = []

        def task():
            result.append(1)
            return "done"

        future = lane.enqueue(task)
        assert future.result(timeout=5) == "done"
        assert result == [1]

    def test_lane_queue_respects_max_concurrency(self):
        """测试 Lane 队列遵守最大并发数"""
        from src.concurrency.lane import LaneQueue
        import threading
        import time

        lane = LaneQueue("test", max_concurrency=2)
        active_count = []
        max_seen = [0]

        def task():
            with threading.Lock():
                active_count.append(len(active_count))
                max_seen[0] = max(max_seen[0], len(active_count))
            time.sleep(0.1)
            with threading.Lock():
                active_count.pop()
            return "done"

        # 入队3个任务，但最大并发为2
        futures = [lane.enqueue(task) for _ in range(3)]

        for f in futures:
            f.result(timeout=5)

        assert max_seen[0] == 2  # 最多同时运行2个


class TestDeliveryQueue:
    """测试投递队列"""

    def test_enqueue_and_deliver(self, tmp_path):
        """测试入队和投递"""
        from src.delivery.queue import DeliveryQueue

        queue = DeliveryQueue(queue_dir=tmp_path / ".delivery")

        delivery_id = queue.enqueue("email", "test@example.com", "测试内容", "测试主题")
        assert delivery_id is not None

        pending = queue.load_pending()
        assert len(pending) == 1
        assert pending[0].channel == "email"
        assert pending[0].to == "test@example.com"

    def test_ack_removes_delivery(self, tmp_path):
        """测试确认后删除投递"""
        from src.delivery.queue import DeliveryQueue

        queue = DeliveryQueue(queue_dir=tmp_path / ".delivery")

        delivery_id = queue.enqueue("email", "test@example.com", "测试内容")
        queue.ack(delivery_id)

        pending = queue.load_pending()
        assert len(pending) == 0


class TestRetryLogic:
    """测试重试逻辑"""

    def test_classify_failure_rate_limit(self):
        """测试失败分类 - 速率限制"""
        from src.resilience.retry import classify_failure, FailoverReason

        exc = Exception("rate limit exceeded")
        assert classify_failure(exc) == FailoverReason.rate_limit

        exc = Exception("429 Too Many Requests")
        assert classify_failure(exc) == FailoverReason.rate_limit

    def test_classify_failure_auth(self):
        """测试失败分类 - 认证"""
        from src.resilience.retry import classify_failure, FailoverReason

        exc = Exception("authentication failed")
        assert classify_failure(exc) == FailoverReason.auth

        exc = Exception("401 Unauthorized")
        assert classify_failure(exc) == FailoverReason.auth


class TestBindingTable:
    """测试路由绑定表"""

    def test_default_binding(self):
        """测试默认路由"""
        from src.gateway.binding import BindingTable

        bt = BindingTable()
        bt.add_default("qa")

        agent_id, binding = bt.resolve(channel="cli")
        assert agent_id == "qa"

    def test_peer_id_binding(self):
        """测试 peer_id 路由"""
        from src.gateway.binding import BindingTable, Binding

        bt = BindingTable()
        bt.add_default("qa")
        bt.add(Binding(agent_id="summarizer", tier=1, match_key="peer_id", match_value="feishu:oc123"))

        # 应该匹配更具体的绑定
        agent_id, binding = bt.resolve(channel="feishu", peer_id="oc123")
        assert agent_id == "summarizer"


class TestSerializeContentBlock:
    """测试内容块序列化"""

    def test_serialize_text_block(self):
        """测试文本块序列化"""
        from src.agents.base import serialize_content_block

        class MockTextBlock:
            type = "text"
            text = "Hello"

        result = serialize_content_block(MockTextBlock())
        assert result == {"type": "text", "text": "Hello"}

    def test_serialize_thinking_block(self):
        """测试思考块序列化"""
        from src.agents.base import serialize_content_block

        class MockThinkingBlock:
            type = "thinking"
            thinking = "Let me think..."

        result = serialize_content_block(MockThinkingBlock())
        assert result == {"type": "thinking", "text": "Let me think..."}

    def test_extract_text_from_content(self):
        """测试从内容列表提取文本"""
        from src.agents.base import extract_text_from_content

        class MockTextBlock:
            def __init__(self, text):
                self.text = text

        blocks = [
            MockTextBlock("Hello "),
            MockTextBlock("World"),
        ]

        result = extract_text_from_content(blocks)
        assert result == "Hello World"


class TestRAGStore:
    """测试 RAGStore 最新批次读取"""

    def test_get_latest_returns_only_latest_batch_sorted_by_rank(self):
        from src.tools.chromadb import RAGStore

        class FakeCollection:
            def get(self, where=None):
                return {
                    "metadatas": [
                        {
                            "repo_id": "old/repo",
                            "repo_name": "old/repo",
                            "crawl_date": "2026-04-14",
                            "crawl_ts": "2026-04-14T09:00:00",
                            "crawl_batch_id": "2026-04-14T09:00:00.000001",
                            "stars": 10,
                            "today_stars": 1,
                            "since": "daily",
                            "rank": 1,
                            "url": "https://github.com/old/repo",
                        },
                        {
                            "repo_id": "latest/repo-b",
                            "repo_name": "latest/repo-b",
                            "crawl_date": "2026-04-15",
                            "crawl_ts": "2026-04-15T09:00:00",
                            "crawl_batch_id": "2026-04-15T09:00:00.000002",
                            "stars": 30,
                            "today_stars": 3,
                            "since": "daily",
                            "rank": 2,
                            "url": "https://github.com/latest/repo-b",
                        },
                        {
                            "repo_id": "latest/repo-a",
                            "repo_name": "latest/repo-a",
                            "crawl_date": "2026-04-15",
                            "crawl_ts": "2026-04-15T09:00:00",
                            "crawl_batch_id": "2026-04-15T09:00:00.000002",
                            "stars": 40,
                            "today_stars": 4,
                            "since": "daily",
                            "rank": 1,
                            "url": "https://github.com/latest/repo-a",
                        },
                    ],
                    "documents": [
                        "old doc",
                        "latest b doc",
                        "latest a doc",
                    ],
                }

        store = RAGStore.__new__(RAGStore)
        store.collection = FakeCollection()

        results = store.get_latest(limit=10)

        assert [item["repo_name"] for item in results] == ["latest/repo-a", "latest/repo-b"]
        assert all(item["crawl_batch_id"] == "2026-04-15T09:00:00.000002" for item in results)

    def test_add_project_deduplicates_latest_and_snapshots_only_on_content_change(self):
        from src.tools.chromadb import RAGStore

        class FakeCollection:
            def __init__(self):
                self.records = {}

            def get(self, ids=None, where=None):
                if ids is not None:
                    matched = [self.records[i] for i in ids if i in self.records]
                    return {
                        "ids": [item["id"] for item in matched],
                        "documents": [item["document"] for item in matched],
                        "metadatas": [item["metadata"] for item in matched],
                    }

                matched = []
                for record in self.records.values():
                    metadata = record["metadata"]
                    if where and any(metadata.get(k) != v for k, v in where.items()):
                        continue
                    matched.append(record)

                return {
                    "ids": [item["id"] for item in matched],
                    "documents": [item["document"] for item in matched],
                    "metadatas": [item["metadata"] for item in matched],
                }

            def upsert(self, documents, ids, metadatas):
                for doc, doc_id, meta in zip(documents, ids, metadatas):
                    self.records[doc_id] = {"id": doc_id, "document": doc, "metadata": meta}

            def add(self, documents, ids, metadatas):
                for doc, doc_id, meta in zip(documents, ids, metadatas):
                    if doc_id in self.records:
                        raise ValueError("duplicate id")
                    self.records[doc_id] = {"id": doc_id, "document": doc, "metadata": meta}

            def count(self):
                return len(self.records)

        base_project = {
            "repo_id": "owner/repo",
            "repo_name": "owner/repo",
            "description": "desc v1",
            "language": "Python",
            "stars": 100,
            "today_stars": 10,
            "since": "daily",
            "rank": 1,
            "topics": ["ai"],
            "url": "https://github.com/owner/repo",
            "crawl_date": "2026-04-15",
            "crawl_ts": "2026-04-15T10:00:00",
            "crawl_batch_id": "batch-1",
            "readme": "hello world",
        }

        store = RAGStore.__new__(RAGStore)
        store.collection = FakeCollection()

        first = store.add_project(dict(base_project))
        second = store.add_project(dict(base_project, stars=101, today_stars=11, rank=2, crawl_batch_id="batch-2"))
        third = store.add_project(dict(base_project, description="desc v2", crawl_batch_id="batch-3"))

        assert first["status"] == "new"
        assert first["snapshot_added"] is True
        assert second["status"] == "unchanged"
        assert second["snapshot_added"] is False
        assert third["status"] == "updated"
        assert third["snapshot_added"] is True

        latest_record = store.collection.records["latest::owner/repo"]
        assert latest_record["metadata"]["record_type"] == "latest"
        assert latest_record["metadata"]["crawl_batch_id"] == "batch-3"
        snapshot_ids = [record_id for record_id in store.collection.records if record_id.startswith("snapshot::owner/repo::")]
        assert len(snapshot_ids) == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
