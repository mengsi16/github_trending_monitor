import os
import yaml
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional
from dotenv import load_dotenv

@dataclass
class AgentConfig:
    model: str = "claude-sonnet-4-20250514"
    max_tokens: int = 32768

@dataclass
class TeamConfig:
    id: str
    name: str
    channels: List[str] = field(default_factory=list)
    feishu_chat_id: str = ""
    email: str = ""

@dataclass
class BotFeishuConfig:
    """单个 Bot 的飞书配置"""
    app_id: str
    app_secret: str
    bot_open_id: str = ""

@dataclass
class BotConfig:
    """多 Bot 配置"""
    id: str                    # 唯一标识 (如 "tech-bot", "invest-bot")
    name: str                  # 显示名称 (如 "技术团队Bot")
    feishu: BotFeishuConfig    # 飞书配置
    personality: str = "tech"  # 性格配置 (tech/invest/content/product)
    agent: str = "qa"          # 绑定的 Agent

@dataclass
class Config:
    agents: dict = field(default_factory=dict)
    github_api_url: str = "https://api.github.com"
    github_top_n: int = 20
    chromadb_dir: str = "./workspace/.chromadb"
    retry_backoff: List[int] = field(default_factory=lambda: [5, 25, 120, 600])
    max_retries: int = 5
    teams: List[TeamConfig] = field(default_factory=list)
    bots: List[BotConfig] = field(default_factory=list)  # 多 Bot 配置
    crawler_cron: str = "0 9 * * *"
    summarizer_cron: str = "30 9 * * *"

def load_config() -> Config:
    # 首先加载 .env 文件
    load_dotenv()

    # 使用 __file__ 计算项目根目录的路径
    config_path = Path(__file__).parent.parent / "config.yaml"
    if not config_path.exists():
        return Config()

    with open(config_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    data = data or {}

    agents = {}
    for name, cfg in data.get("agents", {}).items():
        agents[name] = AgentConfig(**cfg)

    teams = [TeamConfig(**t) for t in data.get("teams", [])]

    # 解析多 Bot 配置
    bots = []
    for bot_data in data.get("bots", []):
        feishu_cfg = BotFeishuConfig(**bot_data.get("feishu", {}))
        bot_cfg = BotConfig(
            id=bot_data.get("id", ""),
            name=bot_data.get("name", ""),
            feishu=feishu_cfg,
            personality=bot_data.get("personality", "tech"),
            agent=bot_data.get("agent", "qa"),
        )
        bots.append(bot_cfg)

    return Config(
        agents=agents,
        github_api_url=data.get("github", {}).get("api_url", "https://api.github.com"),
        github_top_n=data.get("github", {}).get("top_n", 20),
        chromadb_dir=data.get("chromadb", {}).get("persist_directory", "./workspace/.chromadb"),
        retry_backoff=data.get("delivery", {}).get("retry_backoff", [5, 25, 120, 600]),
        max_retries=data.get("delivery", {}).get("max_retries", 5),
        teams=teams,
        bots=bots,
        crawler_cron=data.get("cron", {}).get("crawler_time", "0 9 * * *"),
        summarizer_cron=data.get("cron", {}).get("summarizer_time", "30 9 * * *"),
    )

# 全局配置实例
config = load_config()
