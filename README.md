<div align="center">

# GitHub Trending Monitor

*自动追踪 GitHub 热榜，保留项目版本历史，并按团队视角生成总结与问答。*

[![Python](https://img.shields.io/badge/Python-3.12-blue)](https://www.python.org/)
[![Architecture](https://img.shields.io/badge/Architecture-ReAct%20Agent-green)](README.md)
[![Channels](https://img.shields.io/badge/Channels-Email%20%7C%20Feishu%20%7C%20CLI-orange)](README.md)
[![Storage](https://img.shields.io/badge/Storage-ChromaDB%20%2B%20SQLite-red)](README.md)

[简体中文](README.md) | [English](README_en.md)

> 面向技术、投资、内容、产品四类角色的 GitHub Trending 智能监控、总结、问答与变化分析系统。

</div>

---

## 项目概览

GitHub Trending Monitor 是一个以 Agent 为核心的 GitHub 热榜监控系统。它会抓取 Trending 项目、补充仓库详情与 README、将结果写入 ChromaDB，并基于不同团队的关注点生成总结、回复问答、分析项目变化，再通过 CLI、邮件或飞书交付给用户。

这个项目的重点不是单次抓取，而是把“热榜信息流”沉淀成一个可查询、可比较、可追踪变化的知识库。相比只保存当日快照的方案，当前实现更强调以下两点：

- **去重与历史保留**：同一个仓库不会被重复无意义写入；只有内容发生变化时才保留新的历史快照。
- **面向使用场景的输出**：技术、投资、内容、产品团队可以使用同一份底层数据，但得到不同风格的结论与回答。

---

## 核心能力

### 热榜抓取与版本化存储

系统会抓取 GitHub Trending 项目，并为每个仓库维护两类记录：

| 记录类型 | 作用 |
|------|------|
| `latest` | 当前最新版本，只保留一条，用于日常检索和总结 |
| `snapshot` | 仅在描述、语言、Topics、README 等内容发生变化时新增，用于保留历史 |

当前去重策略基于 `repo_id` 和 `content_signature`：

- **同仓库首次出现**：记为 `new`，写入 `latest`，同时创建首个 `snapshot`
- **仅指标变化**：记为 `unchanged`，只更新 `latest` 中的排名、星数、批次等动态字段
- **内容变化**：记为 `updated`，更新 `latest`，并新增一条 `snapshot`

默认检索和问答只面向 `latest` 记录，避免历史版本污染当前 RAG 结果。README 内容在入库和签名计算时统一截断到 **8000 字**，控制向量长度与重复噪声。

### 多团队总结与自动化投递

系统内置四个团队视角，每个团队共享一套抓取数据，但拥有不同的总结重点：

| 团队 | 关注重点 |
|------|----------|
| 技术团队 | 技术栈、架构、实现思路、工程价值 |
| 投资团队 | 商业潜力、社区热度、增长信号、竞争态势 |
| 内容团队 | 可传播亮点、叙事角度、内容素材 |
| 产品团队 | 用户场景、产品定位、体验与趋势 |

总结可以按团队单独生成，也可以一次生成全部团队版本，并通过邮箱、飞书或 CLI 输出。系统也支持 crawl 与 summarize 组成统一流水线，减少手工串联操作。

### 自然语言问答与 RAG 检索

项目内置 QA Agent，支持针对历史热榜数据做自然语言问答，而不只是生成 summarize。当前问答链路包含：

- **RAG 检索**：基于 ChromaDB 对 `latest` 记录进行语义搜索
- **轻量 Query 改写**：`rag_search` 会自动提取 repo 名、清洗停用词、构造多个检索变体并合并去重
- **会话持久化**：QA 会话保存在 SQLite 中，可继续历史对话
- **自动补数**：当知识库为空或不足时，QA 可以请求触发爬虫更新

这意味着系统已经具备“先查 RAG，再回答用户问题”的能力，而不仅仅是 summarize-only 的管道。

### 项目变化分析

除了保存历史，系统还可以分析“这个项目相比上一个版本发生了什么变化”。当前变化分析聚焦于**内容变化**，而不是指标曲线：

- **描述变化**
- **语言变化**
- **Topics 变化**
- **README 变化**

最新批次的 crawl 结果会输出 `new / updated / unchanged` 统计；同时，`rag_analyze_changes` 工具可以分析单个仓库或最近一批变化项目，为问答和后续扩展提供基础能力。

### 多 Bot、多渠道与会话隔离

系统支持多个飞书 Bot 共享同一进程运行，但使用不同的 `app_id`、personality 与会话上下文。邮件、飞书、CLI 共用同一套 Agent 与路由体系。

- **多 Bot 路由**：通过 `account_id` 将不同飞书机器人路由到对应 Agent
- **多渠道接入**：CLI、Email、Feishu 使用统一的消息分发逻辑
- **会话隔离**：每个 QA 实例维护独立会话，不互相污染
- **失败重试**：消息投递支持队列、重试和失败持久化

---

## 数据与检索策略

为了兼顾“保留历史”和“检索干净”，当前实现采用如下约束：

| 目标 | 策略 |
|------|------|
| 避免同仓库重复堆积 | 用 `repo_id` 标识唯一仓库 |
| 只在内容变化时保留历史 | 用 `content_signature` 判定版本变化 |
| 防止 RAG 被旧版本干扰 | `search()` 与 `get_latest()` 默认只查 `record_type=latest` |
| 让变化可解释 | 在 metadata 中保留描述、语言、topics、README 签名等字段 |

这套设计适合“知识库 + 变化分析”的场景；如果未来需要做星数曲线、排名时序分析，可以在不破坏现有结构的前提下增加独立的 metrics history 存储层。

---

## 交互方式

### CLI 命令

当前 CLI 入口为 `python src/main.py`，启动后可使用以下命令：

| 命令 | 说明 |
|------|------|
| `/crawl` | 手动触发抓取流程，CLI 中默认会继续执行自动总结 |
| `/summarize [team]` | 生成全部团队或指定团队总结 |
| `/compact [N]` | 手动压缩上下文，保留最近 N 轮对话 |
| `/sessions` | 查看历史会话 |
| `/session <id>` | 恢复指定会话 |
| `/new` | 创建新会话 |
| `/status` | 查看 QA Agent 的 Circuit Breaker 状态 |
| `/send ...` | 将总结、爬取结果或自定义消息发送到邮箱或飞书 |
| `/quit` | 退出程序 |

### 邮件与飞书指令

| 渠道 | 指令 |
|------|------|
| 邮件 | `summarize`、`crawl`、`ask <问题>`、`help` |
| 飞书 | `@机器人 summarize`、`@机器人 crawl`、`@机器人 ask <问题>`、`@机器人 help` |

问答请求最终都会进入 QA Agent，因此可以直接通过自然语言询问某个项目是什么、有哪些热门项目、最近什么变了等问题。

---

## 快速开始

### 1. 安装依赖

推荐使用 Python 3.12。

```bash
pip install -r requirements.txt
```

### 2. 准备配置文件

- 复制 `config.example.yaml` 为 `config.yaml`
- 复制 `.env.example` 为 `.env`

最少需要配置：

- **LLM**：`ANTHROPIC_API_KEY`
- **可选兼容网关**：`ANTHROPIC_BASE_URL`
- **GitHub**：`GITHUB_TOKEN`（可选，但建议配置）
- **邮件**：SMTP / IMAP 相关配置（如需邮件收发）
- **飞书**：App 凭证与 Webhook 配置（如需飞书机器人）

### 3. 配置 MCP 工具（可选）

系统支持通过 [MCP（Model Context Protocol）](https://modelcontextprotocol.io) 加载外部工具，例如 Playwright 浏览器自动化。

复制示例配置并按需修改：

```bash
cp mcp_servers_example.json mcp_servers.json
```

内置示例为 Playwright MCP，启用后 QA Agent 可直接控制浏览器。需先安装依赖：

```bash
npm install -g @playwright/mcp
```

`mcp_servers.json` 中的服务器在系统启动时自动连接，工具动态注册到 QA Agent 的工具调用链中。若不需要任何 MCP 工具，跳过此步即可，系统正常运行不受影响。

### 4. 配置团队与 Bot

`config.yaml` 中可以定义团队、Bot、定时任务与存储目录。下面是简化示例：

```yaml
github:
  top_n: 20

chromadb:
  persist_directory: "./workspace/.chromadb"

teams:
  - id: "tech"
    name: "技术团队"
    channels: ["email", "feishu"]
    feishu_chat_id: "oc_xxx"
    email: "your_email@example.com"

bots:
  - id: "tech-bot"
    name: "技术团队Bot"
    feishu:
      app_id: "cli_xxxxxxxxxxxxxxxx"
      app_secret: "your_app_secret_here"
      bot_open_id: "oc_xxxxxxxxxxxxxxxx"
    personality: "tech"
    agent: "qa"

cron:
  crawler_time: "0 9 * * *"
  summarizer_time: "30 9 * * *"
```

### 5. 启动系统

```bash
python src/main.py
```

启动后你可以先执行 `/crawl`，再继续使用 `/summarize` 或直接提问进行验证。

---

## 架构概览

系统以 Agent + Gateway + Tooling 组合实现：

| 组件 | 作用 |
|------|------|
| `CrawlerAgent` | 抓取 Trending、补充详情和 README、写入 ChromaDB |
| `SummarizerAgent` | 基于团队 Prompt 生成总结 |
| `QAAgent` | 调用 RAG 工具回答问题，维护会话历史 |
| `Gateway` | 负责消息路由、频道接入与 Agent 分发 |
| `DeliveryRunner` | 异步发送邮件/飞书消息，支持重试 |
| `SQLiteSessionStore` | 保存 QA 会话与上下文压缩结果 |
| `RAGStore` | 管理 ChromaDB 中的 `latest` / `snapshot` 项目记录 |
| `MCPLoader` | 在启动时连接 MCP 服务器，将其工具动态注入 Agent 工具链 |

在实现上，系统沿用了 ReAct Agent 循环、工具调用、上下文压缩和 Circuit Breaker 机制，以便在长对话、外部依赖失败和多渠道接入的情况下保持稳定。

---

## 依赖环境

当前 `requirements.txt` 中的主要依赖包括：

- `anthropic`
- `chromadb`
- `python-dotenv`
- `pyyaml`
- `httpx`
- `apscheduler`
- `beautifulsoup4`

MCP 工具（可选，需要 Node.js 环境）：

- `@playwright/mcp`：浏览器自动化（`npm install -g @playwright/mcp`）
- `@modelcontextprotocol/server-filesystem`：本地文件系统访问

如果你只是本地验证 CLI + RAG，至少需要保证 LLM、ChromaDB 持久化目录和基础 Python 依赖正常可用。
