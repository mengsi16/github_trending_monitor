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
from src.scheduler import scheduler, setup_cron_jobs
from src.delivery import DeliveryRunner
from src.concurrency import LaneManager

# 全局状态
gateway = Gateway()
delivery_runner = DeliveryRunner()
lane_manager = LaneManager()
main_lane = lane_manager.get_or_create("main", max_concurrency=1)
background_lane = lane_manager.get_or_create("background", max_concurrency=1)
running = True
email_channel = None  # 邮件通道全局实例
feishu_channel = None  # 飞书通道全局实例

def signal_handler(sig, frame):
    global running
    print("\nShutting down...")
    running = False
    scheduler.stop()
    delivery_runner.stop()

def init_agents():
    """初始化 Agents"""
    crawler = CrawlerAgent("crawler")
    qa = QAAgent("qa")
    summarizer = SummarizerAgent("summarizer")

    # 注册到 Gateway
    gateway.register_agent("crawler", crawler)
    gateway.register_agent("qa", qa)
    gateway.register_agent("summarizer", summarizer)

    # 注册到 AgentRegistry（支持 Agent 间通信）
    registry.register("crawler", crawler)
    registry.register("qa", qa)
    registry.register("summarizer", summarizer)

    return crawler, qa, summarizer

def init_channels():
    """初始化 Channels"""
    global email_channel, feishu_channel

    cli = CLIChannel()
    gateway.register_agent("cli", cli)

    # 飞书 (支持发送和接收)
    if os.getenv("FEISHU_APP_ID"):
        feishu_channel = FeishuChannel()
        delivery_runner.register_sender("feishu", feishu_channel.send)
        # 启动长连接客户端（推荐，不需要公网 URL）
        # 如需使用 Webhook 模式，设置环境变量 FEISHU_USE_RPC=false
        feishu_channel.start_rpc_client()

    # 邮件 (支持发送和接收)
    if os.getenv("SMTP_HOST") or os.getenv("IMAP_USER"):
        email_channel = EmailChannel()
        delivery_runner.register_sender("email", email_channel.send)

    return cli

def run_crawler():
    """运行爬虫"""
    print("Running crawler...")
    crawler = gateway.agent_factory.get("crawler")
    if crawler:
        try:
            result = crawler.run_crawl()
            print(f"Crawler result: {result[:200]}...")
        except Exception as e:
            print(f"Crawler error: {e}")

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
    summarizer = gateway.agent_factory.get("summarizer")
    if summarizer:
        try:
            if team_id:
                # 为指定团队生成总结
                team = next((t for t in config.teams if t.id == team_id), None)
                team_name = team.name if team else team_id
                print(f"\n{'='*50}")
                print(f"团队: {team_name} (风格)")
                print(f"{'='*50}")
                summary = summarizer.generate_summary(team_id)
                print(summary[:10000] if len(summary) > 10000 else summary)

                # 入队投递
                if team:
                    for channel in team.channels:
                        if channel == "email" and team.email:
                            delivery_runner.enqueue("email", team.email, summary, f"GitHub 热榜 - {team.name}")
                        if channel == "feishu" and team.feishu_chat_id:
                            delivery_runner.enqueue("feishu", team.feishu_chat_id, summary)
            else:
                # 为所有团队生成总结
                summaries = summarizer.generate_all_summaries()
                # 打印到控制台
                for tid, summary in summaries.items():
                    team = next((t for t in config.teams if t.id == tid), None)
                    team_name = team.name if team else tid
                    print(f"\n{'='*50}")
                    print(f"团队: {team_name}")
                    print(f"{'='*50}")
                    print(summary[:10000] if len(summary) > 10000 else summary)
                    print()

                    # 同时入队投递
                    if team:
                        for channel in team.channels:
                            if channel == "email" and team.email:
                                delivery_runner.enqueue("email", team.email, summary, f"GitHub 热榜 - {team.name}")
                            if channel == "feishu" and team.feishu_chat_id:
                                delivery_runner.enqueue("feishu", team.feishu_chat_id, summary)
        except Exception as e:
            print(f"Summarizer error: {e}")


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
        summarizer = gateway.agent_factory.get("summarizer")
        if summarizer:
            try:
                # 尝试从主题或内容中提取团队
                team_id = None
                for team in config.teams:
                    if team.id in text_lower or team.name in text_lower:
                        team_id = team.id
                        break

                if team_id:
                    response = summarizer.generate_summary(team_id)
                else:
                    # 生成所有团队的总结
                    summaries = summarizer.generate_all_summaries()
                    response = "GitHub 热榜总结:\n\n"
                    for tid, summary in summaries.items():
                        team = next((t for t in config.teams if t.id == tid), None)
                        team_name = team.name if team else tid
                        response += f"=== {team_name} ===\n{summary}\n\n"

                response_subject = "GitHub 热榜总结"
            except Exception as e:
                response = f"生成总结失败: {str(e)}"
        else:
            response = "Summarizer Agent 未初始化"

    # 2. crawl 命令 - 立即爬取
    elif "crawl" in text_lower or "爬取" in text_lower or "刷新" in text_lower:
        print("[Email] 执行 crawl 命令")
        crawler = gateway.agent_factory.get("crawler")
        if crawler:
            try:
                result = crawler.run_crawl()
                response = f"爬取完成!\n\n{result[:2000]}" if len(result) > 2000 else f"爬取完成!\n\n{result}"
                response_subject = "爬取结果"
            except Exception as e:
                response = f"爬取失败: {str(e)}"
        else:
            response = "Crawler Agent 未初始化"

    # 3. help 命令 - 帮助信息
    elif "help" in text_lower or "帮助" in text_lower or "?" in text:
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

支持的团队: """ + ", ".join([t.name for t in config.teams])
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
                response = f"处理失败: {str(e)}"
        else:
            response = "QA Agent 未初始化，请联系管理员"

    # 发送回复
    if response and email_channel:
        try:
            email_channel.send(sender, response, subject=response_subject)
            print(f"[Email] 已回复 {sender}")
        except Exception as e:
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
        summarizer = gateway.agent_factory.get("summarizer")
        if summarizer:
            try:
                team_id = None
                for team in config.teams:
                    if team.id in text_lower or team.name in text_lower:
                        team_id = team.id
                        break

                if team_id:
                    response = summarizer.generate_summary(team_id)
                else:
                    summaries = summarizer.generate_all_summaries()
                    response = "GitHub 热榜总结:\n\n"
                    for tid, summary in summaries.items():
                        team = next((t for t in config.teams if t.id == tid), None)
                        team_name = team.name if team else tid
                        response += f"=== {team_name} ===\n{summary}\n\n"

                response_subject = "GitHub 热榜总结"
            except Exception as e:
                response = f"生成总结失败: {str(e)}"
        else:
            response = "Summarizer Agent 未初始化"

    # 2. crawl 命令
    elif "crawl" in text_lower or "爬取" in text_lower or "刷新" in text_lower:
        print("[Feishu] 执行 crawl 命令")
        crawler = gateway.agent_factory.get("crawler")
        if crawler:
            try:
                result = crawler.run_crawl()
                response = f"爬取完成!\n\n{result[:2000]}" if len(result) > 2000 else f"爬取完成!\n\n{result}"
                response_subject = "爬取结果"
            except Exception as e:
                response = f"爬取失败: {str(e)}"
        else:
            response = "Crawler Agent 未初始化"

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
                response = f"处理失败: {str(e)}"
        else:
            response = "QA Agent 未初始化，请联系管理员"

    # 发送回复
    if response and feishu_channel:
        try:
            # 发送到消息来源的 peer_id
            target = peer_id if is_group else sender
            feishu_channel.send(target, response)
            print(f"[Feishu] 已回复到 {target}")
        except Exception as e:
            print(f"[Feishu] 回复失败: {e}")


def start_email_polling(interval: int = 30):
    """启动邮件轮询线程"""
    global email_channel, running

    if not email_channel:
        print("[Email] 邮件通道未初始化，跳过轮询")
        return

    # 检查 IMAP 是否配置
    if not email_channel.imap_user:
        print("[Email] IMAP_USER 未配置，跳过邮件接收")
        return

    print(f"[Email] 启动邮件轮询 (间隔 {interval} 秒)")

    def poll():
        while running:
            try:
                messages = email_channel.receive_all()
                for msg in messages:
                    handle_email_request(msg)
            except Exception as e:
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

    # 初始化
    print("Initializing...")
    crawler, qa, summarizer = init_agents()
    cli = init_channels()

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
    print("Teams: tech, invest, content, product")
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
    valid_teams = {"tech", "invest", "content", "product"}

    # CLI 主循环
    while running:
        message = cli.receive()
        if message is None:
            break

        if message.text.startswith("/"):
            parts = message.text.strip().split()
            cmd = parts[0].lower()
            team_arg = parts[1].lower() if len(parts) > 1 else None

            if cmd == "/crawl":
                run_crawler()
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
                    print("  # teams 可选: tech, invest, content, product")
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
                        crawler = gateway.agent_factory.get("crawler")
                        if not crawler:
                            print("Crawler 未初始化")
                            continue

                        result = crawler.run_crawl()
                        success = send_to_target(target, result)
                        if success:
                            print(f"已发送爬取结果到 {target}")
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
    print("Goodbye!")

if __name__ == "__main__":
    main()
