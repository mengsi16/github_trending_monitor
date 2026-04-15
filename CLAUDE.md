# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 最高优先级约束：默认只能添加 Tools，禁止随意改动主体

- **默认扩展路径**：后续新增功能，默认只能通过 `src/tools/` 下新增或增强 Tools 实现。
- **禁止随意改主体**：除非需求明确要求改变 Agent 主流程、消息路由、频道接入、调度、会话机制、配置结构或程序入口，否则不得修改主体代码。
- **必须先证明 Tools 不够**：如果确实需要改主体，必须先说明为什么只加 Tools 无法完成，再进行最小范围改动。
- **优先最小改动**：能通过新增 Tool handler、补充 Tool schema、增强 Tool 内部存储/检索逻辑解决的问题，不得下沉到 Agent、Gateway、Channel、Scheduler 等主体模块。

## 主体定义（默认不得变动）

以下目录与文件属于本项目主体，默认禁止为“新增功能”而直接修改：

### 目录级主体

- **`src/agents/`**：Agent 主循环、Prompt 装配、会话接入、Agent 间通信骨架
- **`src/channels/`**：CLI / Email / Feishu 渠道接入与消息收发
- **`src/gateway/`**：消息绑定、路由分发、多 Bot 映射
- **`src/delivery/`**：异步投递、失败重试、落盘机制
- **`src/scheduler/`**：Cron 调度与 Heartbeat 主动任务
- **`src/sessions/`**：会话持久化、恢复、守卫逻辑
- **`src/intelligence/`**：上下文压缩、Prompt/Memory 相关能力
- **`src/resilience/`**：熔断、重试等韧性机制
- **`src/concurrency/`**：Lane 并发隔离机制

### 文件级主体

- **入口与配置**
  - `src/main.py`
  - `src/config.py`
- **Agents**
  - `src/agents/base.py`
  - `src/agents/crawler.py`
  - `src/agents/qa.py`
  - `src/agents/summarizer.py`
  - `src/agents/registry.py`
  - `src/agents/circuit_breaker.py`
- **Channels**
  - `src/channels/base.py`
  - `src/channels/cli.py`
  - `src/channels/email.py`
  - `src/channels/feishu.py`
- **Gateway**
  - `src/gateway/binding.py`
  - `src/gateway/routing.py`
- **Delivery**
  - `src/delivery/queue.py`
  - `src/delivery/runner.py`
- **Scheduler**
  - `src/scheduler/cron.py`
  - `src/scheduler/heartbeat.py`
- **Sessions**
  - `src/sessions/store.py`
  - `src/sessions/sqlite_store.py`
  - `src/sessions/guard.py`
- **Intelligence**
  - `src/intelligence/compactor.py`
  - `src/intelligence/memory.py`
  - `src/intelligence/prompt.py`
- **Resilience**
  - `src/resilience/circuit_breaker.py`
  - `src/resilience/retry.py`
- **Concurrency**
  - `src/concurrency/lane.py`

### 允许优先扩展的区域

- **`src/tools/`**：功能新增的第一落点
- **`src/tools/__init__.py`**：Tool 聚合与暴露

## 如何添加 Tools（新增功能的标准方式）

当需要新增能力时，默认按下面路径实施，不要先改主体：

1. **先判断归属**
   - GitHub 拉取相关能力，优先放到 `src/tools/github.py`
   - RAG / 存储 / 检索 / 变化分析，优先放到 `src/tools/chromadb.py`
   - Agent 间请求，优先放到 `src/tools/agent_tools.py`
   - 邮件 / 飞书辅助能力，优先放到 `src/tools/email.py` 或 `src/tools/feishu.py`

2. **在 Tool 模块内实现 handler**
   - 新增 `tool_xxx(...)` 函数
   - 需要复杂逻辑时，把复杂逻辑收敛到 Tool 内部 helper / store 方法，不要把逻辑塞进 Agent 主体

3. **注册 Tool schema 与 handler**
   - 在对应模块的 `*_TOOLS` 中增加 schema
   - 在对应模块的 `*_HANDLERS` 中增加 handler 映射

4. **通过 `src/tools/__init__.py` 暴露**
   - 如果 QA / Summarizer / 其他 Agent 需要使用该 Tool，再在 `src/tools/__init__.py` 聚合到对应工具集
   - 默认优先补 Tool 集，不要先改 Agent 逻辑

5. **只有在下面情况才允许动主体**
   - 需要新增新的命令入口或交互协议
   - 需要改变 Agent 主循环、Prompt 结构或会话策略
   - 需要改变路由、调度、投递、渠道生命周期
   - 需要调整配置 schema、初始化流程或系统级依赖关系

6. **改主体时必须记录理由**
   - 改了哪些主体文件
   - 为什么只加 Tools 做不到
   - 改动范围为何已经最小化

## 项目概述

GitHub Trending Monitor 是一个 AI Agent 监控系统，用于爬取 GitHub 热榜项目、生成团队定制化总结，并通过多渠道（邮件、飞书、CLI）推送给用户。同时支持用户使用自然语言查询历史热榜数据、对同仓库做去重与版本化存储，并分析项目内容变化。

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

## 最近一次迭代遇到的问题与解决方式

### 1. `/crawl` 会清空旧内容，知识库无法保留历史

- **问题**：爬虫路径里会重建 Chroma collection，导致旧内容被删除。
- **解决**：停止在 crawl 主路径中重置 collection，改为保留历史记录；存储模型调整为 `latest` + `snapshot` 双记录结构。
- **结果**：当前知识库既能保留历史，又能保证日常检索只面向最新版本。

### 2. 同一个仓库会被重复无脑写入

- **问题**：之前没有基于 repo 级别去重，也没有内容变化判断。
- **解决**：在 `src/tools/chromadb.py` 中引入 `repo_id`、`content_signature`、`record_type`、`change_status` 等机制。
- **结果**：同仓库现在分为 `new / updated / unchanged` 三类；只有内容变化时才新增 `snapshot`。

### 3. `summarize` 没有在 crawl 后默认触发，且报错不够可见

- **问题**：crawl 与 summarize 没有被统一成稳定流水线，失败时日志信息也不够明确。
- **解决**：把 crawl 后自动 summarize 的逻辑串起来，并补充更清晰的异常输出与日志记录。
- **结果**：CLI / 消息入口的 crawl 流程更符合预期，失败时更容易定位。

### 4. QA 不是 summarize-only，但缺少更稳的 RAG 检索增强

- **问题**：QA 原本可以用 RAG 回答问题，但检索主要依赖原始 query，缺少轻量 query 改写。
- **解决**：保持 Agent 主体不变，把 query 改写、多变体检索、结果合并去重都放进 `src/tools/chromadb.py` 的 Tool 层。
- **结果**：现在 QA 仍然走原主体，但已经具备更实用的 `RAG search -> answer` 能力。

### 5. 只想要项目变化分析，不想引入指标时序复杂度

- **问题**：需要分析项目内容变化，但不需要星数/排名曲线系统。
- **解决**：继续走 Tool-first 路径，在 `src/tools/chromadb.py` 中新增变化分析能力与 `rag_analyze_changes` Tool。
- **结果**：可以分析单个项目或最近一批项目的描述、语言、Topics、README 变化，而无需改 Agent 主体。

### 6. README 截断阈值不一致

- **问题**：不同位置对 README 的截断长度不一致，容易导致签名判断和返回结果不统一。
- **解决**：统一为 8000 字截断，并在存储与 GitHub Tool 层同步规则。
- **结果**：README 入库、签名计算和直接读取的行为保持一致。

### 7. 测试时暴露出循环导入问题

- **问题**：`src.tools` 与 `src.agents.registry` 的导入链在测试中触发循环依赖。
- **解决**：将 `src/tools/agent_tools.py` 中的 registry 获取改为懒加载。
- **结果**：测试恢复稳定，且没有扩大主体改动范围。

### 8. 这次迭代验证出的开发原则

- **结论**：去重、版本化存储、变化分析、轻量 query 改写，这些能力都可以优先通过 Tools 落地。
- **要求**：后续继续遵守“默认只加 Tools，不改主体”的原则；如果要改主体，必须先论证 Tools 路径失效。

## 架构

### 核心组件

- **Agents** (`src/agents/`): BaseAgent, CrawlerAgent, QAAgent, SummarizerAgent
  - ReAct 循环模式，支持工具调用
  - 三层上下文压缩（Micro → Auto → Full）
  - 熔断器实现容错

- **Channels** (`src/channels/`): CLI, Email, Feishu
  - InboundMessage 数据类统一消息格式
  - 邮件支持 SMTP 发送和 IMAP 接收（轮询）
  - **多 Bot 支持**：FeishuChannel 管理多个 Bot 实例

- **Gateway** (`src/gateway/`): 消息路由到对应 Agent
  - **5 层 BindingTable 路由**：T1(peer_id) → T2(guild_id) → T3(account_id) → T4(channel) → T5(default)
  - `add_bot_binding()` 方法支持 T3 级别 account_id 路由

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

### 多 Bot 架构

系统支持同时运行多个独立的飞书 Bot，每个 Bot 有不同的 personality：

```
用户 → Bot A (app_id=cli_xxx1, personality=tech) → qa_tech-bot Agent
用户 → Bot B (app_id=cli_xxx2, personality=invest) → qa_invest-bot Agent
```

**配置方式** (`config.yaml`):
```yaml
bots:
  - id: "tech-bot"
    name: "技术团队Bot"
    feishu:
      app_id: "cli_xxx1"
      app_secret: "xxx"
      bot_open_id: "oc_xxx1"
    personality: "tech"
    agent: "qa"
  - id: "invest-bot"
    name: "投资团队Bot"
    feishu:
      app_id: "cli_xxx2"
      app_secret: "xxx"
      bot_open_id: "oc_xxx2"
    personality: "invest"
    agent: "qa"
```

**路由优先级**:
1. T1: peer_id (最具体 - 特定用户/会话)
2. T2: guild_id (群组级别)
3. T3: account_id ← 多 Bot 关键：每个 Bot 有唯一 app_id
4. T4: channel (平台级别)
5. T5: default (最不具体 - 默认路由)

**Personality 类型**:
- `tech` - 技术团队风格
- `invest` - 投资团队风格
- `content` - 内容团队风格
- `product` - 产品团队风格

### 配置

- `config.yaml`: 团队配置（tech/invest/content/product）、Bot 配置、Agent 配置、Cron 调度
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

- 支持多 Bot：通过 `bots` 配置或环境变量 `FEISHU_APP_ID`/FEISHU_APP_SECRET
- 每个 Bot 独立的长连接客户端 (`start_rpc_client_for()`)
- 机器人需加入群聊后，通过群 ID (chat_id) 发送消息
- 配置 `feishu_chat_id` 到 config.yaml 的 teams 中
- 默认使用长连接模式（RPC），不需要公网 URL
- 群聊需要 @机器人 才能触发 Agent
- 日志级别可通过环境变量 `FEISHU_LOG_LEVEL` 控制（DEBUG/INFO/WARNING/ERROR）

### 环境变量

```bash
# 飞书应用凭证（单 Bot 模式）
FEISHU_APP_ID=cli_a9xxxx7exxx
FEISHU_APP_SECRET=你的AppSecret
FEISHU_BOT_OPEN_ID=oc_xxx  # 机器人 ID

# 多 Bot 模式使用 config.yaml 中的 bots 配置

# 接收模式（默认 true 为长连接）
FEISHU_USE_RPC=true

# 日志级别（默认 INFO）
FEISHU_LOG_LEVEL=INFO
```

### 关键实现文件

| 文件 | 职责 |
|------|------|
| `src/config.py` | BotConfig, BotFeishuConfig 数据类定义 |
| `src/main.py` | `init_agents()` 创建多 Bot QAAgent，`init_channels()` 注册多 Bot |
| `src/channels/feishu.py` | `register_bot()`, `start_rpc_client_for()` 多 Bot 管理 |
| `src/gateway/routing.py` | `add_bot_binding()` T3 级别 account_id 路由 |
| `src/agents/qa.py` | `PERSONALITY_PROMPTS` 定义 4 种性格，`get_personality_prompt()` 生成对应 System Prompt |
