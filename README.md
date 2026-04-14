# GitHub Trending Monitor

## 项目背景与目标

GitHub Trending Monitor 是一个 AI Agent 监控系统，旨在帮助团队自动化追踪 GitHub 热榜项目并生成定制化报告。系统每天定时爬取 GitHub 热门项目，通过 AI 生成符合不同团队需求的总结内容，最终通过邮件、飞书或 CLI 渠道推送给相关人员。同时，系统支持用户以自然语言查询历史热榜数据，形成完整的从信息获取到智能分析的闭环。

本项目的核心价值在于解决信息过载问题——工程师、投资人、内容创作者和产品经理对 GitHub 热榜的关注点完全不同，系统需要针对不同角色生成差异化的摘要内容，而不是千篇一律的列表。

## 功能需求

### 定时任务与数据采集

系统通过 Cron 表达式配置定时任务，默认每天上午 9 点执行爬虫获取 GitHub 热榜数据。爬虫使用 GitHub API 或网页爬取方式获取 Top 20 项目，采集字段包括项目名称、描述、编程语言、star 数量、fork 数量、贡献者数量等基础信息。每次爬取结果存入 ChromaDB 向量数据库，保留历史记录供后续查询使用。爬虫任务具备熔断机制，当 GitHub API 请求失败或达到限流阈值时，系统不会持续重试导致资源浪费。

### 多团队多风格总结

系统内置四个预定义团队：技术团队、投资团队、内容团队和产品团队，每个团队有独立的推送渠道和总结风格。技术团队关注技术栈新颖性、代码质量和架构设计；投资团队关注项目商业潜力、市场估值和竞争格局；内容团队关注项目传播性和内容创作素材；产品团队关注产品定位、用户体验和发展趋势。总结生成器根据团队 ID 选择对应的 Prompt 模板，生成符合角色预期的结构化摘要。

### 多渠道消息推送

系统支持三种推送渠道：邮件、飞书和 CLI。邮件通道通过 SMTP 协议发送总结报告，支持 IMAP 协议接收用户指令并自动回复，形成完整的邮件交互闭环。飞书通道通过长连接 RPC 模式或 Webhook 模式接收消息，支持群聊和单聊场景，机器人需要被 @ 才能触发处理逻辑。CLI 通道提供交互式命令行界面，用户可直接执行 /crawl、/summarize 等命令。所有渠道共享同一个消息队列系统，发送失败时自动按照指数退避策略重试，最大重试次数为 5 次，失败消息持久化到本地等待人工处理。

### 多机器人独立部署

系统支持同时运行多个独立的飞书机器人，每个机器人有独立的 personality 配置：

- **T3 路由机制**：通过 account_id（飞书 app_id）区分不同 Bot，路由到对应的 Agent 实例
- **独立 Personality**：每个 Bot 可配置 tech/invest/content/product 四种性格之一
- **会话隔离**：每个 Bot 对应独立的 QAAgent 实例，会话互不干扰
- **单进程架构**：多 Bot 共享同一进程，简化部署和维护

配置示例：
```yaml
bots:
  - id: "tech-bot"
    feishu:
      app_id: "cli_xxx1"
      app_secret: "xxx"
    personality: "tech"  # 技术风格
    agent: "qa"
  - id: "invest-bot"
    feishu:
      app_id: "cli_xxx2"
      app_secret: "xxx"
    personality: "invest"  # 投资风格
    agent: "qa"
```

### 自然语言问答

系统内置 QA Agent，基于 RAG（检索增强生成）架构实现历史热榜数据的自然语言查询。用户可以用中文或英文提问，系统从 ChromaDB 中检索相关的热榜记录，结合 LLM 生成回答。QA Agent 维护独立的会话上下文，支持多轮对话，并且提供会话持久化功能，用户下次启动时可以加载历史会话继续对话。

### 上下文压缩

由于 AI 对话有上下文长度限制，系统实现三层上下文压缩机制。Micro 压缩针对单轮对话中的冗余内容进行精简；Auto 压缩在对话进行到一定轮数后自动触发，将早期对话要点提取为摘要；Full 压缩由用户手动触发，输入 /compact N 命令可以强制压缩并保留最近 N 轮完整对话。压缩后的上下文会存入 SQLite 数据库永久保存。

## 技术架构

### Agent 架构

系统采用 ReAct（Reasoning + Acting）模式的 Agent 设计，核心组件包括 CrawlerAgent、SummarizerAgent 和 QAAgent。CrawlerAgent 负责与 GitHub API 交互并处理爬取结果；SummarizerAgent 调用 LLM 生成团队定制化总结；QAAgent 处理用户问答请求并管理会话。Agent 之间通过 AgentRegistry 进行通信，可相互调用对方暴露的工具或方法。

### 网关与路由

Gateway 是消息路由的核心枢纽，接收来自各个 Channel 的 InboundMessage，根据消息来源、目标账户和渠道类型决定将消息路由到哪个 Agent 处理。

系统采用 **5 层 BindingTable 路由机制**，按优先级从高到低：

| 层级 | 匹配键 | 说明 |
|------|--------|------|
| T1 | peer_id | 最具体 - 特定用户/会话 |
| T2 | guild_id | 群组级别 |
| T3 | account_id | Bot 的 app_id，多 Bot 路由关键 |
| T4 | channel | 平台级别（email/feishu/cli） |
| T5 | default | 最不具体 - 默认路由 |

这种分层设计使得系统可以灵活处理各种消息路由场景：从特定 Bot 的消息路由到对应 Agent，到按渠道类型的默认路由。

### 消息投递

Delivery 模块采用异步队列机制处理消息发送。投递任务入队后由 DeliveryRunner 后台执行，支持重试和失败持久化。投递队列使用文件锁实现并发安全，多个投递任务不会相互干扰。SMTP 发送添加 Reply-To 头以支持 Gmail 会话 threading，发送频率限制为最小 5 秒间隔，避免触发垃圾邮件过滤。

### 并发控制

Concurrency 模块通过 LaneManager 实现任务隔离。主 lane 配置 max_concurrency=1，确保 CLI 交互命令顺序执行，避免多个命令同时操作导致的输出混乱。后台 lane 用于定时任务、投递任务等并发场景，各自独立运行互不阻塞。

### 心跳与主动行为 (s07)

系统实现 Heartbeat 心跳机制，支持定时主动任务。心跳使用 Lane 互斥模式——用户输入始终优先于后台任务。当用户正在输入时，后台心跳任务会自动让步，不会打断用户的交互体验。

心跳任务的前置条件包括：时间间隔检查、活跃时间窗口、运行状态检测。任务执行结果通过线程安全的 OutputQueue 异步投递到 REPL，避免阻塞。心跳返回 HEARTBEAT_OK 约定值时表示"无内容报告"，系统会抑制空输出。

## 配置管理

系统配置集中在 config.yaml 文件中，包括四个团队的渠道信息和 Cron 表达式。环境变量通过 .env 文件管理，存放 API 密钥、数据库凭证等敏感信息。config.yaml 中的团队配置支持动态扩展，新的团队只需在配置文件中添加条目即可，无需修改代码。

**多 Bot 配置**：在 `bots` 列表中定义多个飞书机器人，每个机器人有独立的 app_id/app_secret 和 personality：
```yaml
bots:
  - id: "tech-bot"
    name: "技术团队Bot"
    feishu:
      app_id: "cli_xxx"
      app_secret: "xxx"
      bot_open_id: "oc_xxx"
    personality: "tech"
    agent: "qa"
```

## 运行方式

运行主程序前需要配置 .env 文件，填入 ANTHROPIC_API_KEY（必需）、SMTP/IMAP 相关配置（可选）、飞书应用凭证（可选）。执行 python src/main.py 启动系统，程序会初始化所有已配置的渠道和定时任务。CLI 交互模式下可使用 /crawl 手动触发爬取，/summarize [team] 生成指定团队总结，/sessions 查看历史会话。

## 依赖环境

项目基于 Python 3.12 开发，主要依赖包括 anthropic SDK（调用 LLM）、chromadb（向量数据库）、schedule（定时任务）、python-dotenv（环境变量加载）、apscheduler（Cron 定时器）。完整依赖列表见 requirements.txt。
