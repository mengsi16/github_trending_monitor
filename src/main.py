"""GitHub Trending Monitor 主入口"""
import os
import sys
import signal
import threading
import time
import logging
from pathlib import Path
import io

# 配置根日志 - 输出到 workspace/.logs/app.log
log_dir = Path(__file__).parent.parent / "workspace" / ".logs"
log_dir.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s %(name)s %(levelname)s %(message)s',
    handlers=[
        logging.FileHandler(log_dir / "app.log", encoding="utf-8"),
        logging.StreamHandler(sys.stderr)  # CLI 仍会显示 ERROR 级别以上
    ]
)
# 默认把 StreamHandler 级别设为 WARNING，减少 CLI 噪音
for handler in logging.root.handlers:
    if isinstance(handler, logging.StreamHandler):
        handler.setLevel(logging.WARNING)

# 设置 stdout 编码为 UTF-8
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# 添加项目根目录到 path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
from src.config import config
from src.agents import CrawlerAgent, QAAgent, SummarizerAgent
from src.agents.registry import registry
from src.channels import CLIChannel, FeishuChannel, EmailChannel
from src.gateway import Gateway
from src.channels.base import InboundMessage
from src.scheduler import scheduler, setup_cron_jobs, Heartbeat
from src.delivery import DeliveryRunner
from src.concurrency import LaneManager
logger = logging.getLogger("main")

# 全局 Lane 锁 (s07) - 用户输入优先
lane_lock = threading.Lock()
heartbeat: Heartbeat = None

# 全局状态
gateway = Gateway()
delivery_runner = DeliveryRunner()
lane_manager = LaneManager()
main_lane = lane_manager.get_or_create("main", max_concurrency=1)
background_lane = lane_manager.get_or_create("background", max_concurrency=1)
running = True
email_channel = None  # 邮件通道全局实例
feishu_channel = None  # 飞书通道全局实例
heartbeat: Heartbeat = None  # Heartbeat 实例 (s07)

def signal_handler(sig, frame):
    global running
    print("\nShutting down...")
    running = False
    scheduler.stop()
    delivery_runner.stop()
    if heartbeat:
        heartbeat.stop()

def init_agents():
    """初始化 Agents

    支持多 Bot 性格配置：
    - 如果配置了多个 Bot，会创建对应数量的 QAAgent
    - 每个 QAAgent 有不同的 personality
    """
    global gateway

    crawler = CrawlerAgent("crawler")
    summarizer = SummarizerAgent("summarizer")

    # 注册到 Gateway 和 Registry
    gateway.register_agent("crawler", crawler)
    gateway.register_agent("summarizer", summarizer)
    registry.register("crawler", crawler)
    registry.register("summarizer", summarizer)

    # 多 Bot 支持：为每个配置的 Bot 创建独立的 QAAgent
    qa_agents = {}

    # 如果配置了 bots，使用多 Bot 模式
    if config.bots:
        for bot_config in config.bots:
            bot_qa = QAAgent(
                name=f"qa_{bot_config.id}",
                personality=bot_config.personality
            )
            agent_id = f"qa_{bot_config.id}"
            gateway.register_agent(agent_id, bot_qa)
            registry.register(agent_id, bot_qa)
            qa_agents[bot_config.id] = bot_qa

            # 为 Bot 添加路由绑定（T3: account_id）
            gateway.add_bot_binding(
                bot_id=bot_config.id,
                account_id=bot_config.feishu.app_id,
                agent_id=agent_id,
            )

            print(f"[Agent] Registered {bot_config.name} (personality={bot_config.personality}) -> {agent_id}")
    else:
        # 单 Bot 模式（兼容旧配置）
        qa = QAAgent("qa")
        gateway.register_agent("qa", qa)
        registry.register("qa", qa)
        qa_agents["default"] = qa

    # 默认 QA Agent（用于 CLI 等未指定 Bot 的场景）
    default_qa = qa_agents.get("default") or qa_agents.get(list(qa_agents.keys())[0]) if qa_agents else None
    if default_qa:
        gateway.register_agent("qa", default_qa)

    return crawler, default_qa, summarizer

def init_channels():
    """初始化 Channels

    支持多 Bot 模式：
    - 如果配置了 bots，为每个 Bot 注册到 FeishuChannel
    - 也支持环境变量配置的单 Bot 模式（向后兼容）
    """
    global email_channel, feishu_channel

    cli = CLIChannel()
    gateway.register_agent("cli", cli)

    # 飞书 (支持发送和接收)
    feishu_channel = FeishuChannel()

    def _looks_like_placeholder(value: str) -> bool:
        v = (value or "").strip().lower()
        if not v:
            return True
        placeholders = ["xxx", "your_", "placeholder", "example"]
        return any(p in v for p in placeholders)

    # 多 Bot 模式：从 config.bots 注册
    registered_from_config = 0
    if config.bots:
        for bot_config in config.bots:
            app_id = (bot_config.feishu.app_id or "").strip()
            app_secret = (bot_config.feishu.app_secret or "").strip()
            if _looks_like_placeholder(app_id) or _looks_like_placeholder(app_secret):
                print(f"[Feishu] Skip bot {bot_config.id}: invalid/placeholder app_id or app_secret")
                continue

            feishu_channel.register_bot(
                bot_id=bot_config.id,
                app_id=app_id,
                app_secret=app_secret,
                bot_open_id=bot_config.feishu.bot_open_id,
            )
            print(f"[Feishu] Registered bot: {bot_config.name} (app_id={bot_config.feishu.app_id})")
            registered_from_config += 1

    # 当 config.bots 全部被跳过时，回退到 .env 的单 Bot 配置（向后兼容）
    if registered_from_config == 0 and os.getenv("FEISHU_APP_ID"):
        # 单 Bot 模式：从环境变量读取（向后兼容）
        feishu_channel.register_bot(
            bot_id="default",
            app_id=os.getenv("FEISHU_APP_ID"),
            app_secret=os.getenv("FEISHU_APP_SECRET"),
            bot_open_id=os.getenv("FEISHU_BOT_OPEN_ID", ""),
        )
        print("[Feishu] Fallback to env single-bot config")

    # 启动飞书（如果有任何 Bot 注册）
    if feishu_channel._bots:
        delivery_runner.register_sender("feishu", feishu_channel.send)
        # 启动长连接客户端（推荐，不需要公网 URL）
        feishu_channel.start_rpc_client()
    else:
        print("[Feishu] No bots configured, skipping Feishu channel")

    # 邮件 (按能力启用发送/接收，避免半配置导致持续失败)
    has_email_env = any(
        os.getenv(k) for k in ["SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "IMAP_USER", "IMAP_PASSWORD"]
    )
    if has_email_env:
        email_channel = EmailChannel()

        if getattr(email_channel, "smtp_enabled", False):
            delivery_runner.register_sender("email", email_channel.send)
            print("[Email] SMTP sender enabled")
        else:
            print("[Email] SMTP not fully configured, sender disabled")

        if getattr(email_channel, "imap_enabled", False):
            print("[Email] IMAP polling enabled")
        else:
            print("[Email] IMAP not fully configured, polling disabled")

    return cli


def init_heartbeat() -> Heartbeat:
    """初始化 Heartbeat (s07)"""
    global heartbeat

    # 从配置读取心跳间隔和活跃时间
    heartbeat_interval = config.heartbeat_interval if hasattr(config, 'heartbeat_interval') else 60
    heartbeat_active_hours = config.heartbeat_active_hours if hasattr(config, 'heartbeat_active_hours') else (0, 24)

    heartbeat = Heartbeat(
        interval=heartbeat_interval,
        active_hours=heartbeat_active_hours,
        lane_lock=lane_lock  # 共享 Lane 锁
    )

    # 定义心跳执行的函数
    def heartbeat_task() -> str:
        """心跳任务 - 检查是否有待处理事项"""
        # 这里可以扩展为检查各种后台任务的状态
        # 目前作为占位符，返回 HEARTBEAT_OK
        return Heartbeat.HEARTBEAT_OK

    heartbeat.start(heartbeat_task)
    return heartbeat


def _truncate_text(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n\n... [内容已截断]"


def _build_summary_payload(team_id: str = None):
    summarizer = gateway.agent_factory.get("summarizer")
    if not summarizer:
        raise RuntimeError("Summarizer Agent 未初始化")

    if team_id:
        summary = summarizer.generate_summary(team_id)
        return summary, {team_id: summary}

    summaries = summarizer.generate_all_summaries()
    if not summaries:
        raise RuntimeError("未配置任何团队，无法生成总结")

    failed = [
        f"{next((t.name for t in config.teams if t.id == tid), tid)}: {summary}"
        for tid, summary in summaries.items()
        if isinstance(summary, str) and summary.startswith("生成失败:")
    ]
    if failed and len(failed) == len(summaries):
        raise RuntimeError("；".join(failed))

    parts = ["GitHub 热榜总结:\n"]
    for tid, summary in summaries.items():
        team = next((t for t in config.teams if t.id == tid), None)
        team_name = team.name if team else tid
        parts.append(f"=== {team_name} ===\n{summary}\n")

    return "\n".join(parts).strip(), summaries


def _enqueue_summary_deliveries(summaries: dict):
    for tid, summary in summaries.items():
        if not summary or (isinstance(summary, str) and summary.startswith("生成失败:")):
            continue

        team = next((t for t in config.teams if t.id == tid), None)
        if not team:
            continue

        for channel in team.channels:
            if channel == "email" and team.email:
                delivery_runner.enqueue("email", team.email, summary, f"GitHub 热榜 - {team.name}")
            if channel == "feishu" and team.feishu_chat_id:
                delivery_runner.enqueue("feishu", team.feishu_chat_id, summary)


def _run_crawl_pipeline(auto_summarize: bool = False) -> str:
    crawler = gateway.agent_factory.get("crawler")
    if not crawler:
        raise RuntimeError("Crawler Agent 未初始化")

    result = crawler.run_crawl()
    if not auto_summarize:
        return result

    try:
        summary_text, _ = _build_summary_payload()
        return f"{result}\n\n自动总结:\n\n{summary_text}"
    except Exception as e:
        logger.exception("Automatic summarize after crawl failed")
        return f"{result}\n\n自动总结失败: {e}"


def run_crawler(auto_summarize: bool = False):
    """运行爬虫"""
    print("Running crawler...")
    try:
        result = _run_crawl_pipeline(auto_summarize=auto_summarize)
        print(result)
        return result
    except Exception as e:
        logger.exception("Crawler error")
        print(f"Crawler error: {e}")
        return f"Crawler error: {e}"


def run_compact(keep_last: int = 1):
    """运行手动压缩"""
    qa = gateway.agent_factory.get("qa")
    if qa and hasattr(qa, 'full_compact'):
        qa.full_compact(keep_last)
        print(f"手动压缩完成，保留了最近 {keep_last} 轮对话")
    else:
        print("QA Agent 不支持手动压缩")

    # 打印压缩统计
    if qa and hasattr(qa, 'get_compact_stats'):
        stats = qa.get_compact_stats()
        print(f"压缩统计: {stats}")


def run_summarizer(team_id: str = None):
    """运行总结 - 可以指定团队或不指定（所有团队）"""
    print("Running summarizer...")
    try:
        output, summaries = _build_summary_payload(team_id)
        print(output[:10000] if len(output) > 10000 else output)
        _enqueue_summary_deliveries(summaries)
        return output
    except Exception as e:
        logger.exception("Summarizer error")
        print(f"Summarizer error: {e}")
        return f"Summarizer error: {e}"


def run_list_sessions():
    """列出所有会话"""
    qa = gateway.agent_factory.get("qa")
    if not qa or not hasattr(qa, 'list_sessions'):
        print("QA Agent 不支持会话管理")
        return

    sessions = qa.list_sessions(limit=20)
    if not sessions:
        print("暂无历史会话")
        return

    print("\n历史会话列表:")
    print("-" * 60)
    for i, s in enumerate(sessions, 1):
        from datetime import datetime
        time_str = datetime.fromtimestamp(s['updated_at']).strftime('%Y-%m-%d %H:%M')
        print(f"{i}. [{s['session_id'][:8]}] {s['title']} - {time_str} ({s['message_count']} 条消息)")
    print("-" * 60)
    print("使用 /session <编号> 或 /session <会话ID> 继续对话")
    print("使用 /new 开始新会话")


def run_select_session(session_arg: str):
    """选择并加载指定会话"""
    qa = gateway.agent_factory.get("qa")
    if not qa or not hasattr(qa, 'load_session'):
        print("QA Agent 不支持会话管理")
        return None

    # 获取会话列表
    sessions = qa.list_sessions(limit=20)
    if not sessions:
        print("暂无历史会话")
        return None

    # 解析参数
    session_id = None

    # 尝试作为编号处理
    if session_arg.isdigit():
        idx = int(session_arg) - 1
        if 0 <= idx < len(sessions):
            session_id = sessions[idx]['session_id']
    else:
        # 尝试作为会话 ID 处理
        for s in sessions:
            if s['session_id'].startswith(session_arg):
                session_id = s['session_id']
                break

    if not session_id:
        print(f"未找到会话: {session_arg}")
        return None

    # 加载会话
    success = qa.load_session(session_id)
    if success:
        info = qa.session_store.get_session_info(session_id)
        if info:
            print(f"已加载会话: {info['title']}")
            print(f"消息数量: {info['message_count']}")
        return session_id
    else:
        print(f"加载会话失败")
        return None


def run_new_session():
    """创建新会话"""
    qa = gateway.agent_factory.get("qa")
    if not qa or not hasattr(qa, 'create_session'):
        print("QA Agent 不支持会话管理")
        return None

    session_id = qa.create_session()
    print(f"已创建新会话: {session_id}")
    return session_id


def handle_email_request(message: InboundMessage):
    """处理邮件请求并回复"""
    global email_channel

    if not email_channel:
        return

    text = message.text.strip()
    sender = message.sender_id
    subject = message.account_id  # 主题存储在 account_id

    print(f"[Email] 收到来自 {sender} 的邮件: {subject}")

    # 安全过滤：只处理包含命令关键词的邮件，避免回复垃圾邮件
    text_lower = text.lower()
    command_keywords = ["summarize", "总结", "热榜", "crawl", "爬取", "刷新", "help", "帮助", "ask", "问"]

    # 检查是否是命令邮件
    is_command = any(kw in text_lower for kw in command_keywords)

    # 也检查主题是否包含命令
    subject_lower = subject.lower() if subject else ""
    is_command = is_command or any(kw in subject_lower for kw in command_keywords)

    if not is_command:
        print(f"[Email] 忽略非命令邮件 (来自 {sender})")
        return

    print(f"[Email] 内容: {text[:100]}...")

    # 解析命令
    response = None
    response_subject = "回复"

    # 1. summarize 命令 - 获取 GitHub 热榜总结
    if "summarize" in text_lower or "总结" in text_lower or "热榜" in text_lower:
        print("[Email] 执行 summarize 命令")
        try:
            team_id = None
            for team in config.teams:
                if team.id in text_lower or team.name in text_lower:
                    team_id = team.id
                    break

            response, _ = _build_summary_payload(team_id)
            response_subject = "GitHub 热榜总结"
        except Exception as e:
            logger.exception("Email summarize failed")
            response = f"生成总结失败: {str(e)}"

    # 2. crawl 命令 - 立即爬取
    elif "crawl" in text_lower or "爬取" in text_lower or "刷新" in text_lower:
        print("[Email] 执行 crawl 命令")
        try:
            result = _run_crawl_pipeline(auto_summarize=True)
            response = f"爬取完成!\n\n{_truncate_text(result)}"
            response_subject = "爬取与总结结果"
        except Exception as e:
            logger.exception("Email crawl failed")
            response = f"爬取失败: {str(e)}"

    # 3. help 命令 - 帮助信息
    elif "help" in text_lower or "帮助" in text or "?" in text:
        response = """GitHub Trending Monitor - 支持的命令:

1. summarize / 总结 / 热榜
   - 获取 GitHub 热榜总结
   - 可以指定团队: summarize tech

2. crawl / 爬取 / 刷新
   - 立即爬取最新 GitHub 热榜

3. ask <问题>
   - 向 AI 助手提问

4. help / 帮助
   - 显示此帮助信息
"""
        response_subject = "帮助信息"

    # 4. ask 命令 - 向 QA 提问
    elif text_lower.startswith("ask ") or text_lower.startswith("问 "):
        print("[Email] 执行 ask 命令")
        qa = gateway.agent_factory.get("qa")
        if qa:
            try:
                question = text[4:] if text_lower.startswith("ask ") else text[2:]
                response = qa.answer(question)
                response_subject = "AI 回复"
            except Exception as e:
                logger.exception("Email ask failed")
                response = f"处理问题失败: {str(e)}"
        else:
            response = "QA Agent 未初始化"

    # 5. 默认 - 发送到 QA 处理
    else:
        print("[Email] 路由到 QA Agent")
        qa = gateway.agent_factory.get("qa")
        if qa:
            try:
                response = qa.answer(text)
                response_subject = "AI 回复"
            except Exception as e:
                logger.exception("Email QA failed")
                response = f"处理失败: {str(e)}"
        else:
            response = "QA Agent 未初始化，请联系管理员"

    # 发送回复
    if response and email_channel:
        try:
            email_channel.send(sender, response, subject=response_subject)
            print(f"[Email] 已回复 {sender}")
        except Exception as e:
            logger.exception("Email send failed")
            print(f"[Email] 回复失败: {e}")


def handle_feishu_request(message: InboundMessage):
    """处理飞书消息请求"""
    global feishu_channel

    if not feishu_channel:
        return

    text = message.text.strip()
    sender = message.sender_id
    peer_id = message.peer_id
    is_group = message.is_group
    account_id = message.account_id

    print(f"[Feishu] 收到来自 {sender} 的消息: {text[:50]}...")

    # 安全过滤：只处理包含命令关键词的飞书消息
    text_lower = text.lower()
    command_keywords = ["summarize", "总结", "热榜", "crawl", "爬取", "刷新", "help", "帮助", "ask", "问"]

    is_command = any(kw in text_lower for kw in command_keywords)

    if not is_command:
        print(f"[Feishu] 忽略非命令消息")
        return

    # 解析命令
    response = None
    response_subject = "回复"

    # 1. summarize 命令
    if "summarize" in text_lower or "总结" in text_lower or "热榜" in text_lower:
        print("[Feishu] 执行 summarize 命令")
        try:
            team_id = None
            for team in config.teams:
                if team.id in text_lower or team.name in text_lower:
                    team_id = team.id
                    break

            response, _ = _build_summary_payload(team_id)
            response_subject = "GitHub 热榜总结"
        except Exception as e:
            logger.exception("Feishu summarize failed")
            response = f"生成总结失败: {str(e)}"

    # 2. crawl 命令
    elif "crawl" in text_lower or "爬取" in text_lower or "刷新" in text_lower:
        print("[Feishu] 执行 crawl 命令")
        try:
            result = _run_crawl_pipeline(auto_summarize=True)
            response = f"爬取完成!\n\n{_truncate_text(result)}"
            response_subject = "爬取与总结结果"
        except Exception as e:
            logger.exception("Feishu crawl failed")
            response = f"爬取失败: {str(e)}"

    # 3. help 命令
    elif "help" in text_lower or "帮助" in text_lower:
        response = """GitHub Trending Monitor - 支持的命令:

1. summarize / 总结 / 热榜
   - 获取 GitHub 热榜总结

2. crawl / 爬取 / 刷新
   - 立即爬取最新 GitHub 热榜

3. ask <问题>
   - 向 AI 助手提问

4. help / 帮助
   - 显示此帮助信息
"""
        response_subject = "帮助信息"

    # 4. ask 命令
    elif text_lower.startswith("ask ") or text_lower.startswith("问 "):
        print("[Feishu] 执行 ask 命令")
        qa = gateway.agent_factory.get("qa")
        if qa:
            try:
                question = text[4:] if text_lower.startswith("ask ") else text[2:]
                response = qa.answer(question)
                response_subject = "AI 回复"
            except Exception as e:
                logger.exception("Feishu ask failed")
                response = f"处理问题失败: {str(e)}"
        else:
            response = "QA Agent 未初始化"

    # 5. 默认 - 发送到 QA 处理
    else:
        print("[Feishu] 路由到 QA Agent")
        qa = gateway.agent_factory.get("qa")
        if qa:
            try:
                response = qa.answer(text)
                response_subject = "AI 回复"
            except Exception as e:
                logger.exception("Feishu QA failed")
                response = f"处理失败: {str(e)}"
        else:
            response = "QA Agent 未初始化，请联系管理员"

    # 发送回复
    if response and feishu_channel:
        try:
            # 发送到消息来源的 peer_id
            target = peer_id if is_group else sender
            bot_id = feishu_channel.get_bot_id_by_app_id(account_id)
            feishu_channel.send(target, response, bot_id=bot_id)
            print(f"[Feishu] 已回复到 {target} (bot_id={bot_id})")
        except Exception as e:
            logger.exception("Feishu send failed")
            print(f"[Feishu] 回复失败: {e}")


def start_email_polling(interval: int = 30):
    """启动邮件轮询线程"""
    global email_channel, running

    if not email_channel:
        print("[Email] 邮件通道未初始化，跳过轮询")
        return

    # 检查 IMAP 是否可用
    if not getattr(email_channel, "imap_enabled", False):
        print("[Email] IMAP 未完整配置，跳过邮件接收")
        return

    print(f"[Email] 启动邮件轮询 (间隔 {interval} 秒)")

    def poll():
        while running:
            try:
                messages = email_channel.receive_all()
                for msg in messages:
                    handle_email_request(msg)
            except Exception as e:
                logger.exception("Email poll failed")
                print(f"[Email] 轮询错误: {e}")

            time.sleep(interval)

    thread = threading.Thread(target=poll, daemon=True)
    thread.start()


def start_feishu_polling(interval: int = 5):
    """启动飞书消息轮询（从 Webhook 队列获取消息）"""
    global feishu_channel, running

    if not feishu_channel:
        print("[Feishu] 飞书通道未初始化，跳过轮询")
        return

    print(f"[Feishu] 启动消息轮询 (间隔 {interval} 秒)")

    def poll():
        while running:
            try:
                messages = feishu_channel.receive_all()
                for msg in messages:
                    handle_feishu_request(msg)
            except Exception as e:
                logger.exception("Feishu poll failed")
                print(f"[Feishu] 轮询错误: {e}")

            time.sleep(interval)

    thread = threading.Thread(target=poll, daemon=True)
    thread.start()


def main():
    global running

    load_dotenv()

    if not os.getenv("ANTHROPIC_API_KEY"):
        print("Error: ANTHROPIC_API_KEY not set")
        sys.exit(1)

    # 非交互模式是否保持后台常驻（默认 true）
    headless_keep_alive = os.getenv("HEADLESS_KEEP_ALIVE", "true").lower() == "true"

    # 初始化
    print("Initializing...")
    crawler, qa, summarizer = init_agents()
    cli = init_channels()

    # 启动心跳 (s07) - Lane 互斥，用户输入优先
    init_heartbeat()
    print("[Heartbeat] 已启动 (Lane 互斥模式)")

    # 启动投递
    delivery_runner.start()

    # 启动邮件轮询（后台线程）
    start_email_polling(interval=30)

    # 启动飞书轮询（从 Webhook 队列获取消息）
    start_feishu_polling(interval=5)

    # 设置定时任务
    setup_cron_jobs(run_crawler, run_summarizer)
    scheduler.start()

    # 信号处理
    signal.signal(signal.SIGINT, signal_handler)

    print("=" * 50)
    print("GitHub Trending Monitor")
    print("Commands: /crawl, /summarize, /summarize [team], /compact [N], /quit")
    team_ids = [team.id for team in config.teams]
    print(f"Teams: {', '.join(team_ids) if team_ids else '(none configured)'}")
    print("/compact N: 手动压缩，保留最近 N 轮对话 (默认 1)")
    print("/sessions: 列出历史会话")
    print("/session <id>: 继续历史会话")
    print("/new: 创建新会话")
    print("邮件命令: summarize, crawl, ask <问题>, help")
    print("飞书命令: @机器人 发送 summarize/crawl/ask/help (需配置 Webhook)")
    print("/send: 发送消息到邮箱或飞书")
    print("  /send xxx@email.com summarize [teams...]  # 发送到指定邮箱")
    print("  /send mail summarize [team]               # 发送到配置的邮箱")
    print("  /send feishu summarize [team]             # 发送到配置的飞书群")
    print("  /send feishu summarize tech content      # 发送到多个团队")
    print("=" * 50)

    # 当前会话 ID
    current_session_id = None

    # 有效团队列表
    valid_teams = {team.id for team in config.teams}

    # CLI 主循环 (s07 Lane 互斥)
    while running:
        # 1. 先检查 Heartbeat 的待处理输出
        if heartbeat:
            pending = heartbeat.drain_output()
            for output in pending:
                print(f"\n[Heartbeat] {output}\n")

        # 无交互 stdin 场景下，仅保持后台任务运行
        if not sys.stdin.isatty():
            if not headless_keep_alive:
                print("[CLI] Non-interactive stdin detected, exiting (HEADLESS_KEEP_ALIVE=false)")
                break
            time.sleep(0.5)
            continue

        # 2. 获取用户输入 (blocking 获取 Lane)
        # 用户输入始终优先 - 即使 Heartbeat 正在等待，也会被优先处理
        lane_lock.acquire()
        try:
            message = cli.receive()
            if message is None:
                break
        finally:
            lane_lock.release()

        if not message.text.strip():
            continue

        # 3. 处理消息 (与之前相同的逻辑)
        if message.text.startswith("/"):
            parts = message.text.strip().split()
            cmd = parts[0].lower()
            team_arg = parts[1].lower() if len(parts) > 1 else None

            if cmd == "/crawl":
                run_crawler(auto_summarize=True)
            elif cmd == "/summarize":
                if team_arg and team_arg in valid_teams:
                    run_summarizer(team_arg)
                elif team_arg:
                    print(f"Unknown team: {team_arg}")
                    print(f"Valid teams: {', '.join(valid_teams)}")
                else:
                    run_summarizer()
            elif cmd == "/compact":
                try:
                    keep_last = int(team_arg) if team_arg else 1
                    run_compact(keep_last)
                except ValueError:
                    print("Usage: /compact [N] (N 为保留的对话轮数)")
            elif cmd == "/quit":
                break
            elif cmd == "/sessions":
                run_list_sessions()
            elif cmd == "/session":
                if team_arg:
                    current_session_id = run_select_session(team_arg)
                else:
                    print("Usage: /session <会话编号或ID>")
            elif cmd == "/new":
                current_session_id = run_new_session()
            elif cmd == "/status":
                # 查看 Circuit Breaker 状态
                qa = gateway.agent_factory.get("qa")
                if qa and hasattr(qa, 'get_circuit_breaker_status'):
                    print(f"Circuit Breaker 状态: {qa.get_circuit_breaker_status()}")
                else:
                    print("QA Agent 不支持此功能")
            elif cmd == "/send":
                # 命令格式: /send <channel> <command> [teams...]
                # channel: xxx@email.com | mail | feishu | feishu_chat_id
                # command: summarize | crawl | <自定义消息>
                # teams: tech | invest | content | product (可选，默认全部)
                if not team_arg:
                    print("Usage:")
                    print("  # 发送到指定邮箱")
                    print("  /send xxx@example.com summarize [teams...]  # 发送总结")
                    print("  /send xxx@example.com crawl                 # 发送爬取结果")
                    print("  /send xxx@example.com <消息内容>             # 发送自定义消息")
                    print()
                    print("  # 使用默认配置（按 config.yaml）")
                    print("  /send mail summarize [team]               # 发送到配置的邮箱")
                    print("  /send mail crawl                          # 发送爬取结果")
                    print("  /send feishu summarize [team]             # 发送到配置的飞书群")
                    print("  /send feishu crawl                        # 发送爬取结果")
                    print()
                    print(f"  # teams 可选: {', '.join(sorted(valid_teams)) if valid_teams else '(none configured)'}")
                    print("  # 不指定 teams 时，发送所有团队的总结")
                    print("  # 示例: /send mail summarize tech content")
                else:
                    # 解析 channel
                    channel = team_arg.lower()
                    is_email = False
                    is_feishu = False
                    target = None  # 实际发送目标（邮箱地址或飞书群 ID）

                    if "@" in team_arg:
                        # 指定邮箱地址
                        is_email = True
                        target = team_arg
                    elif channel == "mail":
                        # 使用默认配置的邮箱（取第一个团队的邮箱）
                        is_email = True
                        if config.teams and config.teams[0].email:
                            target = config.teams[0].email
                        else:
                            print("错误: config.yaml 中未配置邮箱")
                            continue
                    elif channel == "feishu":
                        # 使用默认配置的飞书群（取第一个团队的飞书群 ID）
                        is_feishu = True
                        if config.teams and config.teams[0].feishu_chat_id:
                            target = config.teams[0].feishu_chat_id
                        else:
                            print("错误: config.yaml 中未配置飞书群 ID")
                            continue
                    else:
                        # 可能是飞书群 ID (oc_xxx)
                        is_feishu = feishu_channel is not None
                        if is_feishu:
                            target = team_arg
                        else:
                            print("错误: 无法识别 channel，请使用邮箱地址、mail、feishu 或飞书群 ID")
                            continue

                    if not is_email and not is_feishu:
                        print("错误: 邮件或飞书通道未配置")
                        continue

                    # 解析剩余部分：command 和 teams
                    remaining = parts[2:] if len(parts) > 2 else []
                    if not remaining:
                        print("Usage: /send <channel> <command> [teams...]")
                        continue

                    content_cmd = remaining[0]
                    team_ids = []

                    # 解析 teams（从 remaining[1:] 中找出有效的团队 ID）
                    for t in remaining[1:]:
                        t_lower = t.lower()
                        if t_lower in valid_teams:
                            team_ids.append(t_lower)
                        elif t in [team.name for team in config.teams]:
                            # 根据团队名称找到团队 ID
                            for team in config.teams:
                                if team.name == t:
                                    team_ids.append(team.id)
                                    break

                    # 发送函数
                    def send_to_target(tgt, content, subject=None):
                        if is_email and email_channel:
                            return email_channel.send(tgt, content, subject=subject or "自定义消息")
                        elif is_feishu and feishu_channel:
                            return feishu_channel.send(tgt, content)
                        return False

                    if content_cmd == "summarize":
                        summarizer = gateway.agent_factory.get("summarizer")
                        if not summarizer:
                            print("Summarizer 未初始化")
                            continue

                        # 生成总结
                        if team_ids:
                            # 发送到指定的一个或多个团队
                            summary = "GitHub 热榜总结:\n\n"
                            for tid in team_ids:
                                s = summarizer.generate_summary(tid)
                                team = next((t for t in config.teams if t.id == tid), None)
                                summary += f"=== {team.name if team else tid} ===\n{s}\n\n"
                        else:
                            # 未指定团队，发送所有团队的总结
                            summaries = summarizer.generate_all_summaries()
                            summary = "GitHub 热榜总结:\n\n"
                            for tid, s in summaries.items():
                                team = next((t for t in config.teams if t.id == tid), None)
                                summary += f"=== {team.name if team else tid} ===\n{s}\n\n"

                        success = send_to_target(target, summary)
                        if success:
                            teams_str = ", ".join(team_ids) if team_ids else "所有团队"
                            print(f"已发送 {teams_str} 总结到 {target}")
                        else:
                            print("发送失败")

                    elif content_cmd == "crawl":
                        try:
                            result = _run_crawl_pipeline(auto_summarize=True)
                        except Exception as e:
                            logger.exception("Send crawl failed")
                            print(f"爬取失败: {e}")
                            continue

                        success = send_to_target(target, _truncate_text(result))
                        if success:
                            print(f"已发送爬取与总结结果到 {target}")
                        else:
                            print("发送失败")

                    else:
                        # 发送自定义消息
                        custom_msg = " ".join(remaining)
                        success = send_to_target(target, custom_msg)
                        if success:
                            print(f"已发送到 {target}")
                        else:
                            print("发送失败")
            else:
                print(f"Unknown command: {cmd}")
            continue

        # 闲聊模式 - 路由到 QA Agent
        agent, err = gateway.route(message)
        if err:
            print(f"Routing error: {err}")
            continue

        try:
            if hasattr(agent, "answer"):
                # 传入当前会话 ID
                response = agent.answer(message.text, session_id=current_session_id)
                # 更新当前会话 ID（如果创建了新会话）
                current_session_id = agent.get_current_session_id()
                agent.send(message.peer_id, response)
            else:
                response, _ = agent.run(message.text)
                agent.send(message.peer_id, response)
        except Exception as e:
            print(f"Agent error: {e}")

    # 清理
    scheduler.stop()
    delivery_runner.stop()
    if heartbeat:
        heartbeat.stop()
    print("Goodbye!")

if __name__ == "__main__":
    main()
