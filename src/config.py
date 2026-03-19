import os
import yaml
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional
from dotenv import load_dotenv

@dataclass
class AgentConfig:
    model: str = "claude-sonnet-4-20250514"
    max_tokens: int = 4096

@dataclass
class TeamConfig:
    id: str
    name: str
    channels: List[str] = field(default_factory=list)
    feishu_chat_id: str = ""
    email: str = ""

@dataclass
class Config:
    agents: dict = field(default_factory=dict)
    github_api_url: str = "https://api.github.com"
    github_top_n: int = 20
    chromadb_dir: str = "./workspace/.chromadb"
    retry_backoff: List[int] = field(default_factory=lambda: [5, 25, 120, 600])
    max_retries: int = 5
    teams: List[TeamConfig] = field(default_factory=list)
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

    agents = {}
    for name, cfg in data.get("agents", {}).items():
        agents[name] = AgentConfig(**cfg)

    teams = [TeamConfig(**t) for t in data.get("teams", [])]

    return Config(
        agents=agents,
        github_api_url=data.get("github", {}).get("api_url", "https://api.github.com"),
        github_top_n=data.get("github", {}).get("top_n", 20),
        chromadb_dir=data.get("chromadb", {}).get("persist_directory", "./workspace/.chromadb"),
        retry_backoff=data.get("delivery", {}).get("retry_backoff", [5, 25, 120, 600]),
        max_retries=data.get("delivery", {}).get("max_retries", 5),
        teams=teams,
        crawler_cron=data.get("cron", {}).get("crawler_time", "0 9 * * *"),
        summarizer_cron=data.get("cron", {}).get("summarizer_time", "30 9 * * *"),
    )

# 全局配置实例
config = load_config()
