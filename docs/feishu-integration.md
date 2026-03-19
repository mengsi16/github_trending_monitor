# 飞书机器人开发完整流程（个人开发者版）

> 本文档整合自飞书开放平台官方文档，帮助个人开发者快速搭建 Agent 与飞书的交互通道。

## 核心参考资料

| 用途 | 链接 |
|------|------|
| 飞书开发者后台 | https://open.feishu.cn/app |
| 创建群 API 文档 | https://open.feishu.cn/document/server-docs/im-v1/chat-content-description/create_json |
| 获取群列表 API | https://open.feishu.cn/document/server-docs/group/chat/list |
| 拉机器人进群 | https://open.feishu.cn/document/server-docs/im-v1/chat-members-description |
| 发送消息 API | https://open.feishu.cn/document/server-docs/im-v1/message-content-description/create_json |
| Webhook 事件订阅 | https://open.feishu.cn/document/server-docs/im-v1/webhook-description |

---

## 一、创建机器人应用

### 1.1 进入开发者后台

访问 **https://open.feishu.cn/app**，使用飞书账号登录。

### 1.2 创建应用

1. 点击「创建应用」
2. 选择「自建应用」
3. 填写应用名称（如 `GitHubMonitor`）和描述
4. 点击创建

### 1.3 获取凭证

创建成功后，在应用详情页获取：
- **App ID**: `cli_xxxxxxxx` 格式
- **App Secret**: 应用密钥

### 1.4 开启机器人能力

1. 进入应用 → 「应用能力」→「机器人」
2. 点击「开通能力」
3. 填写机器人名称和头像
4. 保存并发布

---

## 二、创建群并拉机器人进群

### 2.1 方式一：飞书客户端手动创建（推荐）

1. 打开飞书电脑端
2. 点击「+」→ 「创建群」
3. 设置群名称（如 `GitHubMonitor`）
4. 搜索并添加你的机器人应用
5. 创建完成

### 2.2 方式二：API 创建

调用创建群 API：
```
POST https://open.feishu.cn/open-apis/im/v1/chats
Headers:
  Authorization: Bearer {tenant_access_token}
  Content-Type: application/json
Body:
{
    "name": "GitHubMonitor",
    "user_id_list": [],
    "bot_id_list": ["cli_a930bc37e07a9cc0"]
}
```

---

## 三、获取群 ID (chat_id)

### 3.1 方式一：飞书客户端获取（最简单）

1. 进入群聊 → 点击右上角「...」→「查看群详情」
2. 点击「分享」复制链接
3. 链接格式：`https://xxx.feishu.cn/lark/im?chatId=oc_xxxxxxxxx`
4. `oc_xxxxxxxxx` 就是 chat_id

### 3.2 方式二：API 获取

调用获取群列表 API：
```
GET https://open.feishu.cn/open-apis/im/v1/chats?page_size=50
Headers:
  Authorization: Bearer {tenant_access_token}
```

响应示例：
```json
{
  "code": 0,
  "data": {
    "items": [
      {
        "chat_id": "oc_c3100ef627fe5b39e0e2f51592b9cc5b",
        "name": "GitHubMonitor"
      }
    ]
  }
}
```

---

## 四、配置代码

### 4.1 环境变量 (.env)

```bash
# 飞书应用凭证（必需）
FEISHU_APP_ID=cli_a930bc37e07a9cc0
FEISHU_APP_SECRET=你的AppSecret
FEISHU_BOT_OPEN_ID=oc_xxx  # 机器人 ID（可选）

# 接收消息模式配置
# FEISHU_USE_RPC=true  # 长连接模式（默认，推荐）
# FEISHU_USE_RPC=false # Webhook 模式（需要 ngrok）

# Webhook 配置（仅当 FEISHU_USE_RPC=false 时需要）
# FEISHU_WEBHOOK_HOST=0.0.0.0
# FEISHU_WEBHOOK_PORT=8080
# FEISHU_WEBHOOK_PATH=/webhook/feishu

# 安全配置（可选）
# FEISHU_ENCRYPT_KEY=  # 加密 key，Webhook 模式需要

# 日志配置（可选）
# FEISHU_LOG_LEVEL=INFO  # DEBUG/INFO/WARNING/ERROR，默认 INFO
```

### 4.2 配置文件 (config.yaml)

```yaml
teams:
  - id: "tech"
    name: "技术团队"
    feishu_chat_id: "oc_c3100ef627fe5b39e0e2f51592b9cc5b"  # 填入你的群ID
```

---

## 五、发送消息

### 5.1 代码调用

```python
import httpx
import json

# 获取 tenant_access_token
def get_token(app_id, app_secret):
    resp = httpx.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret}
    )
    return resp.json().get("tenant_access_token")

# 发送文本消息
def send_message(token, chat_id, text):
    resp = httpx.post(
        "https://open.feishu.cn/open-apis/im/v1/messages",
        params={"receive_id_type": "chat_id"},
        headers={"Authorization": f"Bearer {token}"},
        json={
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": text})
        }
    )
    return resp.json().get("code") == 0
```

### 5.2 CLI 命令

在 Agent CLI 中输入：
```
/send feishu summarize    # 发送 GitHub 热榜总结到群
/send feishu crawl        # 爬取并发送最新热榜
/send feishu 你好          # 发送自定义消息
```

---

## 六、接收消息（两种方式）

飞书提供两种接收消息的方式：

| 方式 | 优点 | 缺点 | 适用场景 |
|------|------|------|----------|
| **长连接（推荐）** | 不需要公网 URL，不需要 ngrok | 需安装 SDK | 个人开发、本地运行 |
| Webhook | 支持所有事件类型 | 需要公网 URL | 生产环境、有域名 |

---

### 6.1 长连接模式（推荐）

**优点**：不需要公网 URL，不需要 ngrok，5 分钟配置完成

#### 6.1.1 安装 SDK

```bash
pip install lark-oapi -U
```

#### 6.1.2 配置事件订阅

1. 进入飞书开发者后台 → 应用 → 「事件订阅」
2. 添加事件：`im.message.receive_v1`（接收消息）
3. 在「接收消息」设置中，选择「长连接接收消息」
4. **点击「发布新版本」**（重要！）

#### 6.1.3 环境变量配置

```bash
# 飞书应用凭证
FEISHU_APP_ID=cli_a930bc37e07a9cc0
FEISHU_APP_SECRET=你的AppSecret
FEISHU_BOT_OPEN_ID=oc_xxx  # 机器人 ID（可选）

# 长连接模式（默认开启）
FEISHU_USE_RPC=true  # 设置为 false 可切换到 Webhook 模式
```

#### 6.1.4 启动测试

```bash
python src/main.py
```

控制台应该显示：
```
[Feishu] ========== 长连接模式配置 ==========
[Feishu] 使用飞书 SDK 长连接接收消息
[Feishu] 优点: 不需要公网 URL，不需要 ngrok
[Feishu] ====================================
[Feishu] 长连接客户端已启动，等待消息...
```

然后在飞书群中 @机器人发送 `summarize` 测试。

---

### 6.2 Webhook 模式

#### 6.2.1 使用 ngrok 内网穿透

由于飞书 Webhook 需要公网可访问的 URL，需要使用内网穿透工具：

```bash
# 安装 ngrok (Windows)
# 下载 https://ngrok.com/download

# 启动 ngrok
ngrok http 8080

# 会显示类似这样的公网地址:
# https://xxxx.ngrok-free.app
```

**重要**: 每次重启 ngrok 会得到新的 URL，需要在飞书开发者后台更新。

### 6.2.2 配置事件订阅

1. 进入飞书开发者后台 → 应用 → 「事件订阅」
2. 添加事件：
   - `im.message.receive_v1`（接收消息）
3. 配置订阅 URL：
  - 格式：`https://你的ngrok地址/webhook/feishu`
  - 例如：`https://abcd1234.ngrok-free.app/webhook/feishu`
4. 点击「发布新版本」

### 6.2.3 验证配置

1. 启动程序后，查看控制台输出的 Webhook 地址
2. 在飞书群中 @机器人 发送测试消息
3. 查看控制台是否有 `[Feishu] 收到消息` 日志

### 6.2.4 Webhook 处理代码

```python
from http.server import HTTPServer, BaseHTTPRequestHandler
import json

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)
        payload = json.loads(body)

        # 验证 challenge（首次配置时）
        if "challenge" in payload:
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"challenge": payload["challenge"]}))
            return

        # 处理消息
        event = payload.get("event", {})
        message = event.get("message", {})
        content = json.loads(message.get("content", "{}"))
        text = content.get("text", "")

        # TODO: 处理消息，调用 Agent

        self.send_response(200)
        self.end_headers()

# 启动服务器
server = HTTPServer(("0.0.0.0", 8080), Handler)
server.serve_forever()
```

---

## 七、架构图

```
┌─────────────────────────────────────────────────────────────────────┐
│                          飞书开放平台                                 │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐             │
│  │  开发者后台  │    │    群聊     │    │  长连接/RPC │             │
│  │  app创建/配置│◄──►│ chat_id    │◄──►│ (推荐模式)   │             │
│  └─────────────┘    └─────────────┘    └──────┬──────┘             │
└─────────────────────────────────────────────┼───────────────────────┘
                                              │
                    ┌─────────────────────────┼───────────────────────┐
                    │                         │                       │
                    ▼                         ▼                       ▼
            ┌──────────────┐         ┌──────────────┐       ┌───────────┐
            │ /send feishu │         │  长连接 SDK   │       │  定时任务 │
            │ (CLI发送)    │         │ (接收消息)    │       │  (9:00)   │
            └──────┬───────┘         └──────┬───────┘       └─────┬─────┘
                   │                        │                       │
                   └────────────────────────┼───────────────────────┘
                                            │
                                            ▼
                              ┌─────────────────────────┐
                              │    GitHub Monitor       │
                              │    Agent System         │
                              │  - CrawlerAgent         │
                              │  - SummarizerAgent     │
                              │  - QAAgent             │
                              └─────────────────────────┘
```

---

## 八、常见问题

### Q1: 长连接模式收不到消息？
- 确认已在飞书开发者后台选择「长连接接收消息」
- 确认已点击「发布新版本」
- 检查控制台是否有 `[Feishu RPC] 长连接客户端已启动` 日志
- 检查是否安装了 `lark-oapi` SDK

### Q2: 机器人收不到消息（Webhook 模式）？
- 检查机器人是否已加入群
- 检查事件订阅是否发布新版本
- 检查 ngrok 是否正常运行
- 检查 Webhook URL 是否可访问

### Q3: 发送消息失败？
- 检查 chat_id 是否正确
- 检查 App ID/Secret 是否有效
- 检查应用是否已发布

### Q4: 长连接和 Webhook 模式有什么区别？

| 特性 | 长连接 | Webhook |
|------|--------|---------|
| 需要公网 URL | 否 | 是 |
| 需要 ngrok | 否 | 是 |
| 配置难度 | 简单 | 复杂 |
| 消息延迟 | 即时 | 即时 |
| 适用场景 | 开发/测试 | 生产环境 |

### Q5: 如何切换模式？
- 长连接模式（默认）：设置 `FEISHU_USE_RPC=true`
- Webhook 模式：设置 `FEISHU_USE_RPC=false`

### Q6: 长连接模式收到消息时报错 `'EventMessage' object has no attribute 'sender'`？
这是因为飞书 SDK 返回的 `sender` 是对象不是字典，需要用属性访问。

**错误代码**：
```python
# 错误：sender 是对象，不能用 .get()
sender_id = msg.sender.get("sender_id", {}).get("open_id", "")
```

**正确代码**：
```python
# 正确：使用属性访问
sender_id = ""
if hasattr(msg, 'sender') and msg.sender:
    if hasattr(msg.sender, 'sender_id'):
        sender_id = msg.sender.sender_id.open_id
```

### Q7: 如何控制飞书模块的日志输出？
可以通过环境变量 `FEISHU_LOG_LEVEL` 控制：

```bash
# 默认 INFO 级别
FEISHU_LOG_LEVEL=INFO

# 设置为 DEBUG 可查看详细消息
FEISHU_LOG_LEVEL=DEBUG

# 设置为 WARNING 减少输出
FEISHU_LOG_LEVEL=WARNING
```

同时，Lark SDK 的日志级别也可以在代码中调整：
```python
# 默认 DEBUG 输出很多调试信息
lark.LogLevel.WARNING  # 改为 WARNING 减少输出
```

---

## 九、本项目配置示例

基于 github-trending-monitor 项目：

```bash
# .env 配置
FEISHU_APP_ID=cli_a930bc37e07a9cc0
FEISHU_APP_SECRET=YS2xI9he7jv1rAJqk6fl7ekcvOfkqmKG

# config.yaml 配置
teams:
  - id: "tech"
    feishu_chat_id: "oc_c3100ef627fe5b39e0e2f51592b9cc5b"
```

群 ID 获取结果：`oc_c3100ef627fe5b39e0e2f51592b9cc5b`（来自 GitHubMonitor 群）
