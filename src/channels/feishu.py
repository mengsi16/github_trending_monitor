"""飞书通道 (s04) - 支持长连接接收和 API 发送"""
import os
import json
import httpx
import threading
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
        self.app_id = os.getenv("FEISHU_APP_ID")
        self.app_secret = os.getenv("FEISHU_APP_SECRET")
        self.bot_open_id = os.getenv("FEISHU_BOT_OPEN_ID")
        self.api_base = "https://open.feishu.cn/open-apis"
        self._token = ""
        self._token_expires_at = 0.0

        # 消息队列，用于存储接收到的消息
        self._message_queue: list[InboundMessage] = []
        self._queue_lock = threading.Lock()

        # Webhook 服务器（备用）
        self._server = None
        self._server_thread = None

        # 长连接客户端
        self._rpc_client = None
        self._use_rpc = os.getenv("FEISHU_USE_RPC", "true").lower() == "true"  # 默认使用长连接
        self._running = False

    def _refresh_token(self) -> str:
        if self._token and time.time() < self._token_expires_at:
            return self._token

        try:
            with httpx.Client(timeout=15.0) as client:
                resp = client.post(
                    f"{self.api_base}/auth/v3/tenant_access_token/internal",
                    json={"app_id": self.app_id, "app_secret": self.app_secret}
                )
                data = resp.json()
                if data.get("code") != 0:
                    return ""
                self._token = data.get("tenant_access_token", "")
                self._token_expires_at = time.time() + data.get("expire", 7200) - 300
                return self._token
        except:
            return ""

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

    def _handle_rpc_message(self, data):
        """处理长连接接收到的消息事件"""
        try:
            # 获取消息内容
            msg = data.event.message
            if not msg:
                return

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

            _logger.info(f"收到消息 - chat_id: {chat_id}, chat_type: {chat_type}, user: {sender_id}")
            _logger.debug(f"消息内容: {text[:100]}...")

            # 检查是否 @ 了机器人（群聊时）
            mentions = content.get("mentions", []) if content else []
            if chat_type == "group" and mentions:
                # 检查是否 @ 了机器人
                mentioned = False
                for m in mentions:
                    mid = m.get("id", {})
                    if isinstance(mid, dict):
                        if mid.get("open_id") == self.bot_open_id:
                            mentioned = True
                            break
                    elif isinstance(mid, str) and mid == self.bot_open_id:
                        mentioned = True
                        break
                if not mentioned:
                    _logger.debug("忽略: 消息未 @ 机器人")
                    return

            # 创建 InboundMessage 并添加到队列
            inbound_msg = InboundMessage(
                text=text,
                sender_id=sender_id,
                channel="feishu",
                account_id=self.app_id or "",
                peer_id=sender_id if chat_type == "p2p" else chat_id,
                is_group=(chat_type == "group"),
            )

            with self._queue_lock:
                self._message_queue.append(inbound_msg)

        except Exception as e:
            _logger.error(f"处理消息错误: {e}")

    def start_rpc_client(self):
        """启动长连接客户端（替代 Webhook）

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

        if not self.app_id or not self.app_secret:
            _logger.warning("APP_ID 或 APP_SECRET 未配置，跳过启动")
            return

        # 检查是否启用长连接
        if not self._use_rpc:
            _logger.info("长连接模式未启用，使用 Webhook 模式")
            self.start_webhook_server()
            return

        _logger.info("========== 长连接模式配置 ==========")
        _logger.info("使用飞书 SDK 长连接接收消息")
        _logger.info("优点: 不需要公网 URL，不需要 ngrok")
        _logger.info("====================================")

        # 创建事件处理器
        def on_event(event):
            """事件处理回调"""
            try:
                # im.message.receive_v1 事件类型
                if hasattr(event, 'event_type') and event.event_type == 'im.message':
                    if hasattr(event, 'event') and event.event:
                        self._handle_rpc_message(event.event)
            except Exception as e:
                _logger.error(f"事件处理错误: {e}")

        # 构建事件处理器
        event_handler = lark.EventDispatcherHandler.builder(
            "", ""  # 长连接模式不需要 encrypt_key 和 verification_token
        ).register_p2_im_message_receive_v1(self._handle_rpc_message).build()

        # 启动长连接客户端
        def run_rpc():
            try:
                self._rpc_client = lark.ws.Client(
                    self.app_id,
                    self.app_secret,
                    event_handler=event_handler,
                    log_level=lark.LogLevel.WARNING  # 改为 WARNING 减少日志输出
                )
                _logger.info("长连接客户端已启动，等待消息...")
                self._rpc_client.start()
            except Exception as e:
                _logger.error(f"长连接启动失败: {e}")
                _logger.info("回退到 Webhook 模式...")
                self.start_webhook_server()

        thread = threading.Thread(target=run_rpc, daemon=True)
        thread.start()

    def stop_rpc_client(self):
        """停止长连接客户端"""
        if self._rpc_client:
            try:
                self._rpc_client.stop()
            except:
                pass
            self._rpc_client = None

    def send(self, to: str, text: str, **kwargs) -> bool:
        token = self._refresh_token()
        if not token:
            return False

        try:
            with httpx.Client(timeout=15.0) as client:
                resp = client.post(
                    f"{self.api_base}/im/v1/messages",
                    params={"receive_id_type": "chat_id"},
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
