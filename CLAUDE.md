# CLAUDE.md

本文件为 Claude Code (claude.ai/code) 在本项目中工作时提供指导。

## 项目概述

GitHub Trending Monitor 是一个 AI Agent 监控系统，用于爬取 GitHub 热榜项目、生成团队定制化总结，并通过多渠道（邮件、飞书、CLI）推送给用户。同时支持用户使用自然语言查询历史热榜数据。

## 常用命令

```bash
# 运行主程序
python src/main.py

# CLI 命令（运行时可用）
/crawl                    # 手动触发 GitHub 热榜爬取
/summarize               # 为所有团队生成总结
/summarize [team]        # 为指定团队生成总结 (tech/invest/content/product)
/compact [N]            # 手动压缩上下文，保留最近 N 轮对话（默认 1）
/sessions                # 列出历史会话
/session <id>            # 继续历史会话
/new                     # 创建新会话
/status                  # 查看熔断器状态
/quit                    # 退出应用

# /send 命令格式: /send <channel> <command> [teams...]
# channel: xxx@email.com | mail | feishu | feishu_chat_id
# command: summarize | crawl | <自定义消息>
# teams: tech | invest | content | product (可选)

# 发送到指定邮箱
/send xxx@example.com summarize tech       # 发送 tech 团队总结
/send xxx@example.com summarize tech content  # 发送多个团队总结
/send xxx@example.com crawl                 # 发送爬取结果

# 使用默认配置（按 config.yaml）
/send mail summarize                        # 发送到配置的邮箱（所有团队）
/send mail summarize tech                   # 发送到配置的邮箱（指定团队）
/send feishu summarize                      # 发送到配置的飞书群（所有团队）
/send feishu summarize tech                 # 发送到配置的飞书群（指定团队）
/send feishu crawl                          # 发送爬取结果到飞书
```

## 架构

### 核心组件

- **Agents** (`src/agents/`): BaseAgent, CrawlerAgent, QAAgent, SummarizerAgent
  - ReAct 循环模式，支持工具调用
  - 三层上下文压缩（Micro → Auto → Full）
  - 熔断器实现容错

- **Channels** (`src/channels/`): CLI, Email, Feishu
  - InboundMessage 数据类统一消息格式
  - 邮件支持 SMTP 发送和 IMAP 接收（轮询）

- **Gateway** (`src/gateway/`): 消息路由到对应 Agent
  - 绑定表支持 channel/account/peer 路由

- **Delivery** (`src/delivery/`): 后台消息队列，支持重试
  - 指数退避: [5, 25, 120, 600] 秒
  - 失败消息持久化到 workspace/.delivery/failed/

- **Scheduler** (`src/scheduler/`): Cron 定时任务
  - 默认: 爬虫 9:00，总结 9:30

- **Sessions** (`src/sessions/`): SQLite 会话持久化
  - 支持会话列表、加载、创建

- **Tools** (`src/tools/`): GitHub API、ChromaDB RAG、邮件/飞书辅助函数

- **Intelligence** (`src/intelligence/`): ContextCompactor 上下文压缩

- **Resilience** (`src/resilience/`): CircuitBreaker 熔断、重试逻辑

- **Concurrency** (`src/concurrency/`): LaneManager 任务隔离
  - 主线程 (main lane): max_concurrency=1，确保 CLI 交互顺序执行
  - 后台任务 (background lane): 定时爬取、投递等

### 配置

- `config.yaml`: 团队配置（tech/invest/content/product）、Agent 配置、Cron 调度
- `.env`: API 密钥（ANTHROPIC_API_KEY、GITHUB_TOKEN）、SMTP/IMAP 凭证、飞书令牌

### 团队系统

四个团队，不同风格总结
- `tech` - 技术团队
- `invest` - 投资团队
- `content` - 内容团队
- `product` - 产品团队

### 邮件集成

- SMTP 发送（端口 587，TLS）
- IMAP 接收（端口 993，30 秒轮询）
- 命令关键词过滤：summarize, crawl, help, ask
- 发送频率限制：最小 5 秒间隔
- Reply-To 头实现 Gmail 会话 threading

### 飞书集成

- 需要在飞书开发者后台创建应用并获取 App ID/Secret
- 机器人需加入群聊后，通过群 ID (chat_id) 发送消息
- 配置 `feishu_chat_id` 到 config.yaml 的 teams 中
- 默认使用长连接模式（RPC），不需要公网 URL
- 群聊需要 @机器人 才能触发 Agent
- 日志级别可通过环境变量 `FEISHU_LOG_LEVEL` 控制（DEBUG/INFO/WARNING/ERROR）

### 环境变量

```bash
# 飞书应用凭证
FEISHU_APP_ID=cli_a930bc37e07a9cc0
FEISHU_APP_SECRET=你的AppSecret
FEISHU_BOT_OPEN_ID=oc_xxx  # 机器人 ID

# 接收模式（默认 true 为长连接）
FEISHU_USE_RPC=true

# 日志级别（默认 INFO）
FEISHU_LOG_LEVEL=INFO
```
