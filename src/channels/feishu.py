"""飞书通道 (s04) - 支持长连接接收和 API 发送"""
import os
import json
import httpx
import threading
import asyncio
import hashlib
import hmac
import base64
import logging
import time
import io
import sys
from typing import Optional, Callable
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs
from .base import Channel, InboundMessage

# 飞书 SDK（长连接模式）
try:
    import lark_oapi as lark
    LARK_SDK_AVAILABLE = True
except ImportError:
    LARK_SDK_AVAILABLE = False

# 配置日志
_log_level = os.getenv("FEISHU_LOG_LEVEL", "INFO").upper()
_logger = logging.getLogger("feishu")
_logger.setLevel(getattr(logging, _log_level, logging.INFO))

# 清除已有的 handler（避免重复）
_logger.handlers.clear()

# 添加文件 handler - 输出到 workspace/.logs/feishu.log
try:
    from pathlib import Path
    log_dir = Path(__file__).parent.parent.parent / "workspace" / ".logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_dir / "feishu.log", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)  # 文件记录所有级别
    _formatter = logging.Formatter('[Feishu] %(asctime)s %(levelname)s %(message)s')
    file_handler.setFormatter(_formatter)
    _logger.addHandler(file_handler)
except Exception as e:
    # 如果无法创建文件handler，回退到 StreamHandler
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(logging.Formatter('[Feishu] %(message)s'))
    _logger.addHandler(stream_handler)

# 如果设置了 CLI_LOG_LEVEL=0，抑制 StreamHandler 输出
if os.getenv("CLI_LOG_LEVEL") == "0":
    for handler in _logger.handlers:
        if isinstance(handler, logging.StreamHandler):
            handler.setLevel(logging.CRITICAL)

# 设置编码
if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')


class FeishuChannel(Channel):
    name = "feishu"

    # Webhook 服务器配置
    webhook_host = os.getenv("FEISHU_WEBHOOK_HOST", "0.0.0.0")
    webhook_port = int(os.getenv("FEISHU_WEBHOOK_PORT", "8080"))
    webhook_path = os.getenv("FEISHU_WEBHOOK_PATH", "/webhook/feishu")
    encrypt_key = os.getenv("FEISHU_ENCRYPT_KEY", "")  # 可选的加密 key

    def __init__(self):
        # 多 Bot 支持：存储多个 bot 配置
        # bot_id -> {app_id, app_secret, bot_open_id, rpc_client, token, token_expires_at}
        self._bots: dict = {}
        self._default_bot_id: str = None

        # 单 Bot 兼容：也从环境变量读取（如果配置了）
        app_id = os.getenv("FEISHU_APP_ID")
        app_secret = os.getenv("FEISHU_APP_SECRET")
        bot_open_id = os.getenv("FEISHU_BOT_OPEN_ID")
        if app_id and app_secret:
            self._bots["default"] = {
                "app_id": app_id,
                "app_secret": app_secret,
                "bot_open_id": bot_open_id or "",
                "rpc_client": None,
                "token": "",
                "token_expires_at": 0.0,
            }
            self._default_bot_id = "default"

        self.api_base = "https://open.feishu.cn/open-apis"

        # 消息队列，用于存储接收到的消息
        self._message_queue: list[InboundMessage] = []
        self._queue_lock = threading.Lock()

        # Webhook 服务器（备用）
        self._server = None
        self._server_thread = None
        self._rpc_thread = None
        self._rpc_loop = None

        self._use_rpc = os.getenv("FEISHU_USE_RPC", "true").lower() == "true"  # 默认使用长连接
        self._running = False

    def register_bot(self, bot_id: str, app_id: str, app_secret: str, bot_open_id: str = ""):
        """注册一个新的 Bot 配置（多 Bot 支持）"""
        self._bots[bot_id] = {
            "app_id": app_id,
            "app_secret": app_secret,
            "bot_open_id": bot_open_id,
            "rpc_client": None,
            "token": "",
            "token_expires_at": 0.0,
        }
        if self._default_bot_id is None:
            self._default_bot_id = bot_id
        _logger.info(f"Registered bot: {bot_id} (app_id: {app_id})")

    def _get_bot_config(self, bot_id: str = None) -> dict:
        """获取 Bot 配置"""
        if bot_id is None:
            bot_id = self._default_bot_id
        return self._bots.get(bot_id) or self._bots.get("default", {})

    def get_bot_id_by_app_id(self, app_id: str) -> Optional[str]:
        """根据 app_id 反查 bot_id，用于按入站 Bot 回发消息。"""
        if not app_id:
            return self._default_bot_id

        for bid, bot in self._bots.items():
            if bot.get("app_id") == app_id:
                return bid

        return self._default_bot_id

    def _refresh_token(self, bot_id: str = None) -> str:
        """刷新指定 Bot 的访问令牌"""
        bot = self._get_bot_config(bot_id)
        if not bot:
            return ""

        app_id = bot.get("app_id")
        app_secret = bot.get("app_secret")
        token = bot.get("token", "")
        expires_at = bot.get("token_expires_at", 0.0)

        if token and time.time() < expires_at:
            return token

        try:
            with httpx.Client(timeout=15.0) as client:
                resp = client.post(
                    f"{self.api_base}/auth/v3/tenant_access_token/internal",
                    json={"app_id": app_id, "app_secret": app_secret}
                )
                data = resp.json()
                if data.get("code") != 0:
                    return ""
                token = data.get("tenant_access_token", "")
                expires_at = time.time() + data.get("expire", 7200) - 300
                bot["token"] = token
                bot["token_expires_at"] = expires_at
                return token
        except:
            return ""

    # 兼容旧接口：app_id/app_secret 属性
    @property
    def app_id(self) -> str:
        bot = self._get_bot_config()
        return bot.get("app_id", "")

    @property
    def app_secret(self) -> str:
        bot = self._get_bot_config()
        return bot.get("app_secret", "")

    @property
    def bot_open_id(self) -> str:
        bot = self._get_bot_config()
        return bot.get("bot_open_id", "")

    # -------------------- Webhook 服务器 --------------------

    def _verify_signature(self, timestamp: str, sign: str) -> bool:
        """验证飞书签名（防止伪造请求）

        飞书签名机制：
        1. 拼接 timestamp + "\n" + app_secret
        2. 使用 HMAC-SHA256 计算签名
        3. Base64 编码后与请求中的 sign 对比
        """
        if not self.app_secret:
            _logger.warning("未配置 APP_SECRET，拒绝未签名请求")
            return False

        if not timestamp or not sign:
            _logger.warning("缺少签名参数，拒绝请求")
            return False

        try:
            # 拼接字符串: timestamp\napp_secret
            string_to_sign = f"{timestamp}\n{self.app_secret}"

            # HMAC-SHA256 签名
            hmac_code = hmac.new(
                string_to_sign.encode("utf-8"),
                digestmod=hashlib.sha256
            ).digest()

            # Base64 编码
            computed_sign = base64.b64encode(hmac_code).decode('utf-8')

            # 使用 constant-time 比较防止时序攻击
            if hmac.compare_digest(computed_sign, sign):
                _logger.debug("签名验证通过")
                return True
            else:
                _logger.warning("签名验证失败: 签名不匹配")
                return False

        except Exception as e:
            _logger.error(f"签名验证错误: {e}")
            return False

    def _decrypt(self, encrypt: str) -> dict:
        """解密飞书加密消息"""
        if not encrypt or not self.encrypt_key:
            return {}

        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            from cryptography.hazmat.backends import default_backend

            # Base64 解码 encrypt_key
            key = base64.b64decode(self.encrypt_key)

            # Base64 解码加密字符串
            encrypted_bytes = base64.b64decode(encrypt)

            # AES-ECB 解密
            cipher = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
            decryptor = cipher.decryptor()
            decrypted = decryptor.update(encrypted_bytes) + decryptor.finalize()

            # 去除 PKCS7 填充
            pad_len = decrypted[-1]
            decrypted = decrypted[:-pad_len]

            # 解析 JSON
            content = json.loads(decrypted.decode('utf-8'))
            _logger.debug("消息解密成功")
            return content

        except ImportError:
            _logger.debug("缺少 cryptography 库，使用简化解密")
            return {}
        except Exception as e:
            _logger.error(f"解密失败: {e}")
            return {}

    def _handle_webhook(self, payload: dict) -> Optional[InboundMessage]:
        """处理 webhook 事件"""
        # 验证 challenge（飞书验证 URL 时会发送）
        if "challenge" in payload:
            return None

        # 解析消息
        msg = self.parse_webhook(payload, self.encrypt_key)
        if msg:
            # 添加到消息队列
            with self._queue_lock:
                self._message_queue.append(msg)
        return msg

    def receive(self) -> Optional[InboundMessage]:
        """从消息队列中获取消息"""
        with self._queue_lock:
            if self._message_queue:
                return self._message_queue.pop(0)
        return None

    def receive_all(self) -> list[InboundMessage]:
        """获取所有待处理消息"""
        with self._queue_lock:
            messages = self._message_queue.copy()
            self._message_queue.clear()
        return messages

    def start_webhook_server(self):
        """启动 Webhook 服务器（后台线程）"""
        if not self.app_id:
            _logger.warning("APP_ID 未配置，跳过 Webhook 服务器")
            return

        # 打印配置信息
        _logger.info("========== Webhook 配置信息 ==========")
        _logger.info(f"本地地址: http://{self.webhook_host}:{self.webhook_port}{self.webhook_path}")
        _logger.warning("⚠️  注意: 此地址为内网地址，飞书无法直接访问!")
        _logger.info("")
        _logger.info("解决方案:")
        _logger.info("  1. 使用 ngrok 内网穿透:")
        _logger.info("     ngrok http 8080")
        _logger.info("  2. 在飞书开发者后台配置 Webhook URL:")
        _logger.info("     https://your-ngrok-url.ngrok-free.app/webhook/feishu")
        _logger.info("  3. 在事件订阅中添加: im.message.receive_v1")
        _logger.info("  4. 发布新版本应用")
        _logger.info("=========================================")

        self._running = True

        # 创建自定义 RequestHandler
        channel_ref = self

        class Handler(BaseHTTPRequestHandler):
            def do_post(self):
                # 解析路径和查询参数
                from urllib.parse import urlparse, parse_qs
                parsed = urlparse(self.path)
                path = parsed.path
                query_params = parse_qs(parsed.query)

                if path != channel_ref.webhook_path:
                    self.send_error(404)
                    return

                # 获取签名参数
                timestamp = query_params.get("timestamp", [None])[0]
                sign = query_params.get("sign", [None])[0]

                # 验证签名（challenge 请求除外）
                content_length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(content_length).decode('utf-8')

                try:
                    payload = json.loads(body)

                    # challenge 请求直接返回（URL 验证）
                    if "challenge" in payload:
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/json')
                        self.end_headers()
                        self.wfile.write(json.dumps({"challenge": payload.get("challenge")}).encode())
                        return

                    # 验证签名
                    if not channel_ref._verify_signature(timestamp, sign):
                        _logger.warning("签名验证失败，拒绝请求")
                        self.send_response(403)
                        self.send_header('Content-Type', 'application/json')
                        self.end_headers()
                        self.wfile.write(json.dumps({"code": -1, "msg": "signature verification failed"}).encode())
                        return

                    # 处理消息
                    channel_ref._handle_webhook(payload)

                    self.send_response(200)
                    self.end_headers()

                except Exception:
                    _logger.error("Webhook 处理错误")
                    self.send_error(400)

            def log_message(self, format, *args):
                # 抑制日志输出
                pass

        def run_server():
            try:
                channel_ref._server = HTTPServer(
                    (channel_ref.webhook_host, channel_ref.webhook_port),
                    Handler
                )
                _logger.info(f"Webhook 服务器启动: http://{channel_ref.webhook_host}:{channel_ref.webhook_port}{channel_ref.webhook_path}")
                channel_ref._server.serve_forever()
            except Exception as e:
                _logger.error(f"Webhook 服务器启动失败: {e}")

        self._server_thread = threading.Thread(target=run_server, daemon=True)
        self._server_thread.start()

    def stop_webhook_server(self):
        """停止 Webhook 服务器"""
        self._running = False
        if self._server:
            self._server.shutdown()
            self._server = None
        if self._server_thread:
            self._server_thread = None

    # -------------------- 长连接模式 (RPC) --------------------

    def _handle_rpc_message(self, data, bot_id: str = None):
        """处理长连接接收到的消息事件"""
        try:
            # 获取消息内容
            msg = data.event.message
            if not msg:
                return

            bot = self._get_bot_config(bot_id)
            bot_open_id = bot.get("bot_open_id", "") if bot else ""
            app_id = bot.get("app_id", "") if bot else ""

            chat_id = msg.chat_id
            chat_type = msg.chat_type

            # 修复：sender 是对象不是字典，需要用属性访问
            sender_id = ""
            if hasattr(msg, 'sender') and msg.sender:
                if hasattr(msg.sender, 'sender_id'):
                    sender_id_obj = msg.sender.sender_id
                    if hasattr(sender_id_obj, 'open_id'):
                        sender_id = sender_id_obj.open_id

            # 解析消息内容
            content = json.loads(msg.content) if isinstance(msg.content, str) else msg.content
            text = content.get("text", "") if content else ""

            if not text:
                return

            _logger.info(f"收到消息 - bot: {bot_id}, chat_id: {chat_id}, chat_type: {chat_type}, user: {sender_id}")
            _logger.debug(f"消息内容: {text[:100]}...")

            # 检查是否 @ 了机器人（群聊时）
            mentions = content.get("mentions", []) if content else []
            if chat_type == "group" and mentions:
                # 检查是否 @ 了机器人
                mentioned = False
                for m in mentions:
                    mid = m.get("id", {})
                    if isinstance(mid, dict):
                        if mid.get("open_id") == bot_open_id:
                            mentioned = True
                            break
                    elif isinstance(mid, str) and mid == bot_open_id:
                        mentioned = True
                        break
                if not mentioned:
                    _logger.debug("忽略: 消息未 @ 机器人")
                    return

            # 创建 InboundMessage 并添加到队列
            # 使用 app_id 作为 account_id（用于路由到不同 Bot）
            inbound_msg = InboundMessage(
                text=text,
                sender_id=sender_id,
                channel="feishu",
                account_id=app_id or "",
                peer_id=sender_id if chat_type == "p2p" else chat_id,
                is_group=(chat_type == "group"),
            )

            with self._queue_lock:
                self._message_queue.append(inbound_msg)

        except Exception as e:
            _logger.error(f"处理消息错误: {e}")

    def start_rpc_client(self):
        """启动长连接客户端（替代 Webhook）

        如果注册了多个 Bot，会为每个 Bot 启动独立的 RPC 连接。
        长连接模式优点：
        - 不需要公网 URL
        - 不需要配置 SSL 证书
        - SDK 自动处理鉴权和消息解密
        """
        if not LARK_SDK_AVAILABLE:
            _logger.warning("lark-oapi SDK 未安装，回退到 Webhook 模式")
            _logger.warning("请运行: pip install lark-oapi")
            self.start_webhook_server()
            return

        if not self._bots:
            _logger.warning("没有注册的 Bot，跳过启动")
            return

        # 检查是否启用长连接
        if not self._use_rpc:
            _logger.info("长连接模式未启用，使用 Webhook 模式")
            self.start_webhook_server()
            return

        _logger.info("========== 长连接模式配置 ==========")
        _logger.info("使用飞书 SDK 长连接接收消息")
        _logger.info(f"将启动 {len(self._bots)} 个 Bot 的 RPC 连接")
        _logger.info("====================================")

        if self._rpc_thread and self._rpc_thread.is_alive():
            _logger.info("RPC 长连接线程已在运行，跳过重复启动")
            return

        bot_ids: list[str] = []
        for bot_id in self._bots:
            bot = self._get_bot_config(bot_id)
            if not bot:
                continue
            if not bot.get("app_id") or not bot.get("app_secret"):
                _logger.warning(f"Bot {bot_id} 缺少 app_id 或 app_secret，跳过")
                continue
            bot_ids.append(bot_id)

        if not bot_ids:
            _logger.warning("没有可用的 Bot 配置，无法启动 RPC")
            return

        def run_all_bots_rpc():
            loop = asyncio.new_event_loop()
            self._rpc_loop = loop
            asyncio.set_event_loop(loop)

            try:
                import lark_oapi.ws.client as ws_client_module
                ws_client_module.loop = loop

                async def bootstrap_all():
                    connected = 0

                    for bot_id in bot_ids:
                        bot = self._get_bot_config(bot_id)
                        app_id = bot.get("app_id")
                        app_secret = bot.get("app_secret")

                        def make_message_handler(bid):
                            def handler(data):
                                self._handle_rpc_message(data, bot_id=bid)
                            return handler

                        event_handler = lark.EventDispatcherHandler.builder(
                            "", ""
                        ).register_p2_im_message_receive_v1(make_message_handler(bot_id)).build()

                        try:
                            rpc_client = lark.ws.Client(
                                app_id,
                                app_secret,
                                event_handler=event_handler,
                                log_level=lark.LogLevel.WARNING
                            )
                            bot["rpc_client"] = rpc_client
                            await rpc_client._connect()
                            loop.create_task(rpc_client._ping_loop())
                            connected += 1
                            _logger.info(f"Bot {bot_id} 长连接客户端已启动，等待消息...")
                        except Exception as e:
                            bot["rpc_client"] = None
                            _logger.error(f"Bot {bot_id} 长连接启动失败: {e}")

                    if connected == 0:
                        _logger.error("所有 Bot 的长连接启动均失败")
                        return

                    await ws_client_module._select()

                # 自动重连：当 _select() 因连接断开退出时，指数退避重试
                max_retries = 100  # 几乎无限重试
                base_delay = 2.0
                for attempt in range(max_retries):
                    try:
                        loop.run_until_complete(bootstrap_all())
                    except Exception as e:
                        _logger.error(f"RPC 长连接线程异常: {e}")

                    # 如果 RPC 线程被外部停止，退出循环
                    if not self._rpc_thread or not self._rpc_thread.is_alive():
                        break

                    delay = min(base_delay * (2 ** min(attempt, 6)), 300)  # 最大 5 分钟
                    _logger.warning(
                        f"RPC 长连接断开，{delay:.0f}s 后重连 (attempt={attempt + 1})"
                    )
                    import time
                    time.sleep(delay)

                    # 重建事件循环（旧的已关闭）
                    if loop.is_closed():
                        loop = asyncio.new_event_loop()
                        self._rpc_loop = loop
                        asyncio.set_event_loop(loop)
                        ws_client_module.loop = loop

            except Exception as e:
                _logger.error(f"RPC 长连接线程异常退出: {e}")
            finally:
                self._rpc_loop = None
                asyncio.set_event_loop(None)
                try:
                    loop.close()
                except Exception:
                    pass

        self._rpc_thread = threading.Thread(target=run_all_bots_rpc, daemon=True)
        self._rpc_thread.start()

    def start_rpc_client_for(self, bot_id: str):
        """为指定 Bot 启动长连接客户端

        Args:
            bot_id: Bot ID
        """
        if not LARK_SDK_AVAILABLE:
            _logger.warning("lark-oapi SDK 未安装，无法启动 RPC")
            return

        bot = self._get_bot_config(bot_id)
        if not bot:
            _logger.warning(f"Bot {bot_id} 未注册，跳过")
            return

        app_id = bot.get("app_id")
        app_secret = bot.get("app_secret")

        if not app_id or not app_secret:
            _logger.warning(f"Bot {bot_id} 缺少 app_id 或 app_secret，跳过")
            return

        # 创建事件处理器（绑定到特定 bot_id）
        def make_message_handler(bid):
            def handler(data):
                self._handle_rpc_message(data, bot_id=bid)
            return handler

        event_handler = lark.EventDispatcherHandler.builder(
            "", ""  # 长连接模式不需要 encrypt_key 和 verification_token
        ).register_p2_im_message_receive_v1(make_message_handler(bot_id)).build()

        # 启动长连接客户端
        def run_rpc():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                rpc_client = lark.ws.Client(
                    app_id,
                    app_secret,
                    event_handler=event_handler,
                    log_level=lark.LogLevel.WARNING
                )
                bot["rpc_client"] = rpc_client
                _logger.info(f"Bot {bot_id} 长连接客户端已启动，等待消息...")
                rpc_client.start()
            except Exception as e:
                _logger.error(f"Bot {bot_id} 长连接启动失败: {e}")
            finally:
                try:
                    asyncio.set_event_loop(None)
                    loop.close()
                except Exception:
                    pass

        thread = threading.Thread(target=run_rpc, daemon=True)
        thread.start()

    def stop_rpc_client(self):
        """停止所有长连接客户端"""
        if self._rpc_loop and self._rpc_loop.is_running():
            async def shutdown_clients():
                for bot_id, bot in self._bots.items():
                    rpc_client = bot.get("rpc_client")
                    if rpc_client:
                        try:
                            await rpc_client._disconnect()
                            _logger.info(f"Bot {bot_id} RPC 客户端已停止")
                        except Exception:
                            pass
                        bot["rpc_client"] = None

            try:
                fut = asyncio.run_coroutine_threadsafe(shutdown_clients(), self._rpc_loop)
                fut.result(timeout=5)
            except Exception:
                pass

            try:
                self._rpc_loop.call_soon_threadsafe(self._rpc_loop.stop)
            except Exception:
                pass

            if self._rpc_thread and self._rpc_thread.is_alive():
                self._rpc_thread.join(timeout=2)

            self._rpc_thread = None
            self._rpc_loop = None
            return

        for bot_id, bot in self._bots.items():
            rpc_client = bot.get("rpc_client")
            if rpc_client:
                try:
                    rpc_client.stop()
                    _logger.info(f"Bot {bot_id} RPC 客户端已停止")
                except:
                    pass
                bot["rpc_client"] = None

    def send(self, to: str, text: str, bot_id: str = None, **kwargs) -> bool:
        """发送消息到飞书

        Args:
            to: 接收者 ID (chat_id 或 open_id)
            text: 消息文本
            bot_id: 可选的 Bot ID，不指定则使用默认 Bot
        """
        token = self._refresh_token(bot_id)
        if not token:
            return False

        # 自动识别接收者类型：私聊用户 open_id(ou_)，群聊 chat_id(oc_)
        # 也支持通过 kwargs 显式覆盖 receive_id_type。
        receive_id_type = kwargs.get("receive_id_type")
        if not receive_id_type:
            receive_id_type = "open_id" if isinstance(to, str) and to.startswith("ou_") else "chat_id"

        try:
            with httpx.Client(timeout=15.0) as client:
                resp = client.post(
                    f"{self.api_base}/im/v1/messages",
                    params={"receive_id_type": receive_id_type},
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "receive_id": to,
                        "msg_type": "text",
                        "content": json.dumps({"text": text})
                    }
                )
                data = resp.json()
                return data.get("code", 1) == 0
        except:
            return False

    def _check_mentioned(self, event: dict) -> bool:
        """检查是否 @了机器人"""
        mentions = event.get("message", {}).get("mentions", [])
        for m in mentions:
            mid = m.get("id", {})
            if isinstance(mid, dict):
                if mid.get("open_id") == self.bot_open_id:
                    return True
            elif isinstance(mid, str):
                if mid == self.bot_open_id:
                    return True
        return True  # 私聊默认通过

    def parse_webhook(self, payload: dict, encrypt_key: str = "") -> Optional[InboundMessage]:
        """解析飞书 webhook 事件"""
        # 验证 challenge
        if "challenge" in payload:
            _logger.debug("收到 URL 验证请求")
            return None

        event = payload.get("event", {})
        message = event.get("message", {})
        sender = event.get("sender", {})
        user_id = sender.get("sender_id", {}).get("open_id", "")
        chat_id = message.get("chat_id", "")
        chat_type = message.get("chat_type", "")

        _logger.debug(f"收到消息 - chat_id: {chat_id}, chat_type: {chat_type}, user: {user_id}")

        # 解析消息内容
        raw_content = message.get("content", "{}")
        try:
            content = json.loads(raw_content) if isinstance(raw_content, str) else raw_content
        except:
            content = {}

        text = content.get("text", "")
        _logger.debug(f"消息内容: {text[:100]}...")

        # 群聊需要 @ 检测
        is_group = chat_type == "group"
        if is_group and self.bot_open_id:
            if not self._check_mentioned(event):
                _logger.debug("忽略: 消息未 @ 机器人")
                return None

        if not text:
            _logger.debug("忽略: 消息内容为空")
            return None

        return InboundMessage(
            text=text,
            sender_id=user_id,
            channel="feishu",
            account_id=self.app_id or "",
            peer_id=user_id if chat_type == "p2p" else chat_id,
            is_group=is_group,
        )
