<div align="center">

# GitHub Trending Monitor

*Automatically track GitHub Trending, preserve project version history, and generate team-oriented summaries and answers.*

[![Python](https://img.shields.io/badge/Python-3.12-blue)](https://www.python.org/)
[![Architecture](https://img.shields.io/badge/Architecture-ReAct%20Agent-green)](README_en.md)
[![Channels](https://img.shields.io/badge/Channels-Email%20%7C%20Feishu%20%7C%20CLI-orange)](README_en.md)
[![Storage](https://img.shields.io/badge/Storage-ChromaDB%20%2B%20SQLite-red)](README_en.md)

[简体中文](README.md) | [English](README_en.md)

> An intelligent GitHub Trending monitoring, summarization, QA, and change-analysis system for engineering, investment, content, and product teams.

</div>

---

## Overview

GitHub Trending Monitor is an agent-driven system for tracking GitHub Trending projects. It fetches trending repositories, enriches them with repository metadata and README content, stores them in ChromaDB, and then uses that shared knowledge base to generate team-specific summaries, answer natural-language questions, analyze project changes, and deliver results through CLI, email, or Feishu.

The project is not just about daily snapshots. Its core idea is to turn the GitHub Trending stream into a searchable, comparable, and change-aware knowledge base. Compared with a plain “store today’s list” workflow, the current implementation focuses on two things:

- **Deduplication with history retention**: the same repository is not blindly re-inserted; a new historical snapshot is only created when meaningful content changes.
- **Output tailored to actual consumers**: engineering, investment, content, and product teams use the same underlying data but receive different summaries and answers.

---

## Core Capabilities

### Trending crawl with versioned storage

The system crawls GitHub Trending projects and maintains two record types per repository:

| Record Type | Purpose |
|------|------|
| `latest` | The current state of a repo; only one record is kept for search and summaries |
| `snapshot` | Historical versions, created only when description, language, topics, or README meaningfully change |

The deduplication strategy is based on `repo_id` and `content_signature`:

- **First time seen**: marked as `new`, writes `latest`, and creates the first `snapshot`
- **Only metrics changed**: marked as `unchanged`, updates dynamic fields in `latest` such as rank, stars, and crawl batch
- **Content changed**: marked as `updated`, updates `latest`, and creates a new `snapshot`

By default, retrieval and QA only operate on `latest` records so that historical versions do not pollute current RAG results. README content is truncated to **8000 characters** during storage and signature generation to control vector size and reduce near-duplicate noise.

### Team-specific summaries and automated delivery

The system includes four built-in team perspectives. All of them share the same crawled data, but each one focuses on different outcomes:

| Team | Focus |
|------|-------|
| Tech | stack choice, architecture, implementation quality, engineering value |
| Invest | commercial potential, community traction, growth signals, competitive dynamics |
| Content | story angles, shareable highlights, content hooks, narrative material |
| Product | user scenarios, positioning, user experience, market direction |

Summaries can be generated per team or for all teams at once, then delivered through email, Feishu, or CLI. The system also supports a unified crawl-and-summarize workflow so users do not need to manually stitch the steps together.

### Natural-language QA with multi-source information retrieval

The project already supports natural-language QA over historical trending data; it is not summarize-only. The current QA path uses a **multi-source information retrieval strategy**:

1. **RAG retrieval**: semantic search over `latest` records in ChromaDB
2. **Playwright cache**: page snapshots from Playwright browser visits are automatically saved in `.playwright-mcp/`; the Agent can read cached data with `grep`/`read_file` instead of re-browsing
3. **Playwright web research**: when RAG and cache both lack data, the Agent automatically uses Playwright to visit GitHub pages and fetch live information
4. **Local file access**: the Agent can read project files, search content, and execute shell commands to directly access raw crawled data
5. **Automatic refresh fallback**: when the knowledge base is missing or stale, QA can request a fresh crawl

Additional QA features:

- **Lightweight query rewriting**: `rag_search` extracts repo names, removes stop words, builds multiple retrieval variants, and merges deduplicated results
- **Session persistence**: QA conversations are stored in SQLite and can be resumed later
- **Iteration intervention**: when the agent loop approaches its limit, a wrap-up instruction is injected to prevent infinite tool-call loops
- **Graceful degradation**: if the iteration limit is exceeded, the last available text is returned instead of raising an exception

In practice, this means the system follows a "RAG → cache → web browse → answer" pipeline, instead of only producing scheduled summaries.

### Project change analysis

Beyond keeping history, the system can explain how a project changed compared with its previous content version. The current change analysis focuses on **content changes**, not metrics time series:

- **Description changes**
- **Language changes**
- **Topic changes**
- **README changes**

The latest crawl output includes a `new / updated / unchanged` breakdown. In addition, the `rag_analyze_changes` tool can inspect a single repository or summarize changed projects in the latest crawl batch, which makes the history actually usable for QA and future product extensions.

### Multi-bot, multi-channel, isolated conversations

The system supports multiple Feishu bots running in a single process, each with its own `app_id`, personality, and conversation context. Email, Feishu, and CLI all share the same routing and agent infrastructure.

- **Multi-bot routing**: `account_id` is used to route each Feishu bot to the correct agent instance
- **Multi-channel access**: CLI, email, and Feishu share a common dispatch model
- **Session isolation**: each QA instance maintains separate conversation state
- **Reliable delivery**: outgoing messages use queues, retries, and persisted failure handling

---

## Data and Retrieval Strategy

To balance “keep history” and “keep retrieval clean,” the project currently follows these rules:

| Goal | Strategy |
|------|------|
| Avoid duplicate accumulation for the same repo | Use `repo_id` as the repository identity |
| Preserve history only for meaningful changes | Use `content_signature` to detect content version changes |
| Prevent old versions from degrading RAG | `search()` and `get_latest()` default to `record_type=latest` |
| Make changes explainable | Store description, language, topics, and README signature in metadata |

This design is optimized for a **knowledge base + change analysis** workflow. If you later need star curves or rank time-series analytics, you can add a separate metrics-history layer without breaking the current structure.

---

## Interaction Modes

### CLI commands

The CLI entry point is `python src/main.py`. After startup, the following commands are available:

| Command | Description |
|------|------|
| `/crawl` | Run the crawl flow manually; in CLI this also continues into automatic summarization |
| `/summarize [team]` | Generate summaries for one team or all teams |
| `/compact [N]` | Manually compact context and keep the latest N turns |
| `/sessions` | List saved sessions |
| `/session <id>` | Restore a saved session |
| `/new` | Start a new session |
| `/status` | Show the QA agent's Circuit Breaker status |
| `/send ...` | Send summaries, crawl results, or custom messages to email or Feishu |
| `/quit` | Exit the program |

### Email and Feishu commands

| Channel | Commands |
|------|------|
| Email | `summarize`, `crawl`, `ask <question>`, `help` |
| Feishu | `@bot summarize`, `@bot crawl`, `@bot ask <question>`, `@bot help` |

All QA requests eventually go through the QA Agent, so users can directly ask questions such as what a project is, which repos are trending, or what changed recently.

---

## Quick Start

### 1. Install dependencies

Python 3.12 is recommended.

```bash
pip install -r requirements.txt
```

### 2. Prepare configuration files

- Copy `config.example.yaml` to `config.yaml`
- Copy `.env.example` to `.env`

At minimum, you should configure:

- **LLM access**: `ANTHROPIC_API_KEY`
- **Optional compatible gateway**: `ANTHROPIC_BASE_URL`
- **GitHub**: `GITHUB_TOKEN` (optional, but recommended)
- **Email**: SMTP / IMAP settings if you want mail delivery and mail-driven commands
- **Feishu**: app credentials and webhook settings if you want bot integration

### 3. Configure MCP tools (optional)

The system supports loading external tools via [MCP (Model Context Protocol)](https://modelcontextprotocol.io), such as Playwright browser automation.

Copy the example config and modify as needed:

```bash
cp mcp_servers_example.json mcp_servers.json
```

The built-in example is Playwright MCP. After enabling it, the QA Agent can directly control the browser. Install the dependency first:

```bash
npm install -g @playwright/mcp
```

Page snapshots from Playwright browser visits are automatically saved in the `.playwright-mcp/` directory. The QA Agent checks the cache first before initiating new browser visits, avoiding redundant requests.

Servers defined in `mcp_servers.json` are connected at startup, and their tools are dynamically registered into the QA Agent's tool chain. If you don't need any MCP tools, skip this step — the system works fine without them.

### 4. Workspace file tools

The QA Agent includes built-in workspace file and shell tools (`src/tools/workspace.py`) — no extra configuration needed:

| Tool | Purpose | Safety constraints |
|------|---------|-------------------|
| `read_file` | Read files under the project directory | Restricted to project root |
| `list_dir` | List directory contents | Restricted to project root |
| `grep` | Regex search in files | Restricted to project root, skips >5MB files |
| `bash` | Execute shell commands | PowerShell on Windows, Bash on Linux/macOS; dangerous commands blocked |
| `edit_file` | Edit files (find & replace) | Only `workspace/` and `.playwright-mcp/` files are editable |

### 5. Configure teams and bots

`config.yaml` defines teams, bots, cron schedules, and storage paths. A simplified example:

```yaml
github:
  top_n: 20

chromadb:
  persist_directory: "./workspace/.chromadb"

teams:
  - id: "tech"
    name: "Tech Team"
    channels: ["email", "feishu"]
    feishu_chat_id: "oc_xxx"
    email: "your_email@example.com"

bots:
  - id: "tech-bot"
    name: "Tech Team Bot"
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

### 6. Start the system

```bash
python src/main.py
```

A practical first test is to run `/crawl`, then use `/summarize` or ask a direct question in the same session.

---

## Architecture at a Glance

The system is built around Agents + Gateway + Tools:

| Component | Responsibility |
|------|------|
| `CrawlerAgent` | Fetch trending data, enrich repo details and README, write to ChromaDB |
| `SummarizerAgent` | Generate team-specific summaries from the stored data |
| `QAAgent` | Multi-source information retrieval (RAG + Playwright + file tools), answer questions, manage session history |
| `Gateway` | Route inbound messages and dispatch them to the correct agent |
| `DeliveryRunner` | Send email/Feishu messages asynchronously with retries |
| `SQLiteSessionStore` | Persist QA sessions and compacted conversation state |
| `RAGStore` | Manage `latest` and `snapshot` project records in ChromaDB |
| `MCPLoader` | Connect to MCP servers at startup, dynamically inject their tools into the agent toolchain |
| `WorkspaceTools` | Provide file reading, directory browsing, text search, shell execution, and file editing |

Implementation-wise, the project uses a ReAct-style agent loop, tool invocation, context compaction, and Circuit Breaker protection so that long conversations, unstable external dependencies, and multi-channel delivery remain manageable. The agent loop also features iteration intervention and graceful degradation, and the Feishu WebSocket connection supports automatic reconnection on disconnect.

---

## Dependencies

The current `requirements.txt` includes these main dependencies:

- `anthropic`
- `chromadb`
- `python-dotenv`
- `pyyaml`
- `httpx`
- `apscheduler`
- `beautifulsoup4`

If you only want to validate the local CLI + RAG flow, the minimum requirement is a working LLM configuration, a writable ChromaDB persistence directory, and the Python dependencies above.
