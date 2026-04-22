"""邮件通道 (s04) - 支持 Gmail IMAP 接收和 SMTP 发送"""
import os
import smtplib
import imaplib
import email
import time
import ssl
import socket
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import decode_header
from typing import Optional, List
from .base import Channel, InboundMessage


_logger = logging.getLogger("email_channel")


class EmailChannel(Channel):
    name = "email"

    def __init__(self):
        # SMTP 发邮件配置
        self.smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
        self.smtp_port = int(os.getenv("SMTP_PORT", "587"))
        self.smtp_user = os.getenv("SMTP_USER")
        self.smtp_password = os.getenv("SMTP_PASSWORD")
        self.from_addr = os.getenv("SMTP_FROM", self.smtp_user)
        self.smtp_timeout = int(os.getenv("SMTP_TIMEOUT", "20"))
        self.smtp_backoff_max = int(os.getenv("SMTP_BACKOFF_MAX", "300"))

        # IMAP 收邮件配置 (Gmail)
        self.imap_host = os.getenv("IMAP_HOST", "imap.gmail.com")
        self.imap_port = int(os.getenv("IMAP_PORT", "993"))
        self.imap_user = os.getenv("IMAP_USER")
        self.imap_password = os.getenv("IMAP_PASSWORD")
        self.imap_timeout = int(os.getenv("IMAP_TIMEOUT", "15"))
        self.imap_backoff_max = int(os.getenv("IMAP_BACKOFF_MAX", "300"))

        # 连接失败退避，避免 IMAP 异常时日志刷屏
        self._imap_error_count = 0
        self._imap_next_retry_at = 0.0
        self._imap_last_log_at = 0.0

        # SMTP 连接失败退避，避免持续失败影响主流程
        self._smtp_error_count = 0
        self._smtp_next_retry_at = 0.0
        self._smtp_last_log_at = 0.0

        # 按能力启用，避免半配置状态下频繁失败
        self.smtp_enabled = bool(self.smtp_host and self.smtp_user and self.smtp_password and self.from_addr)
        self.imap_enabled = bool(self.imap_user and self.imap_password)

        # 追踪已处理的邮件，避免重复处理
        # 使用有界集合避免内存泄漏，最多保留 1000 个 ID
        from collections import deque
        self._processed_ids: deque = deque(maxlen=1000)

    def receive(self) -> Optional[InboundMessage]:
        """通过 IMAP 轮询接收邮件"""
        if not self.imap_enabled:
            return None

        if not self._imap_ready():
            return None

        try:
            msg = self._receive_imap()
            self._reset_imap_backoff()
            return msg
        except (
            ssl.SSLError,
            socket.timeout,
            TimeoutError,
            OSError,
            imaplib.IMAP4.abort,
            imaplib.IMAP4.error,
            RuntimeError,
        ) as e:
            self._handle_imap_connection_error(e)
            return None
        except Exception as e:
            _logger.error("IMAP receive error: %s", e)
            return None

    def receive_all(self) -> List[InboundMessage]:
        """接收所有未处理的新邮件"""
        if not self.imap_enabled:
            return []

        if not self._imap_ready():
            return []

        try:
            messages = self._receive_all_imap()
            self._reset_imap_backoff()
            return messages
        except (
            ssl.SSLError,
            socket.timeout,
            TimeoutError,
            OSError,
            imaplib.IMAP4.abort,
            imaplib.IMAP4.error,
            RuntimeError,
        ) as e:
            self._handle_imap_connection_error(e)
            return []
        except Exception as e:
            _logger.error("IMAP receive all error: %s", e)
            return []

    def _imap_ready(self) -> bool:
        return time.time() >= self._imap_next_retry_at

    def _reset_imap_backoff(self) -> None:
        self._imap_error_count = 0
        self._imap_next_retry_at = 0.0

    def _handle_imap_connection_error(self, error: Exception) -> None:
        self._imap_error_count += 1
        backoff = min(5 * (2 ** (self._imap_error_count - 1)), self.imap_backoff_max)
        self._imap_next_retry_at = time.time() + backoff

        # 连续错误仅限频打印，避免刷屏
        now = time.time()
        should_log = self._imap_error_count <= 3 or (now - self._imap_last_log_at) >= 60
        if should_log:
            self._imap_last_log_at = now
            _logger.warning(
                "IMAP connection error: %s. Retrying in %ss (attempt %s)",
                error,
                backoff,
                self._imap_error_count,
            )

    def _connect_imap(self):
        context = ssl.create_default_context()
        mail = imaplib.IMAP4_SSL(
            self.imap_host,
            self.imap_port,
            timeout=self.imap_timeout,
            ssl_context=context,
        )
        mail.login(self.imap_user, self.imap_password)
        typ, _ = mail.select("INBOX")
        if typ != "OK":
            raise RuntimeError("IMAP select INBOX failed")
        return mail

    def _receive_imap(self) -> Optional[InboundMessage]:
        """使用 IMAP 接收单封最新邮件"""
        mail = self._connect_imap()
        try:
            # 搜索未读邮件
            typ, data = mail.search(None, 'UNSEEN')
            if typ != "OK":
                raise RuntimeError("IMAP search UNSEEN failed")
            if not data or not data[0]:
                return None

            mail_ids = data[0].split()
            if not mail_ids:
                return None

            # 取最新的一封
            latest_id = mail_ids[-1]
            msg = self._fetch_mail(mail, latest_id)

            # 标记为已读
            mail.store(latest_id, '+FLAGS', '\\Seen')
            return msg
        finally:
            try:
                mail.logout()
            except Exception:
                pass

    def _receive_all_imap(self) -> List[InboundMessage]:
        """接收所有未读邮件"""
        messages = []

        mail = self._connect_imap()
        try:
            # 搜索未读邮件
            typ, data = mail.search(None, 'UNSEEN')
            if typ != "OK":
                raise RuntimeError("IMAP search UNSEEN failed")
            if not data or not data[0]:
                return messages

            mail_ids = data[0].split()

            for mail_id in mail_ids:
                if mail_id in self._processed_ids:
                    continue

                msg = self._fetch_mail(mail, mail_id)
                if msg:
                    messages.append(msg)
                    self._processed_ids.append(mail_id)

                # 标记为已读
                mail.store(mail_id, '+FLAGS', '\\Seen')

            return messages
        finally:
            try:
                mail.logout()
            except Exception:
                pass

    def _fetch_mail(self, mail, mail_id) -> Optional[InboundMessage]:
        """从 IMAP 获取邮件内容"""
        typ, msg_data = mail.fetch(mail_id, '(RFC822)')
        if typ != "OK" or not msg_data:
            return None

        for response_part in msg_data:
            if isinstance(response_part, tuple):
                msg = email.message_from_bytes(response_part[1])

                # 解析发件人
                sender = email.utils.parseaddr(msg.get("From"))[1]

                # 解析主题
                subject_raw = msg.get("Subject", "")
                if subject_raw:
                    subject, encoding = decode_header(subject_raw)[0]
                    if isinstance(subject, bytes):
                        subject = subject.decode(encoding or 'utf-8', errors='replace')
                else:
                    subject = ""

                # 解析邮件正文
                body = self._parse_body(msg)

                return InboundMessage(
                    text=body,
                    sender_id=sender,
                    channel="email",
                    account_id=subject  # 主题存入 account_id 用于命令解析
                )

        return None

    def _parse_body(self, msg) -> str:
        """解析邮件正文"""
        body = ""

        if msg.is_multipart():
            # 多部分邮件，遍历所有部分
            for part in msg.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get("Content-Disposition"))

                # 优先获取纯文本
                if content_type == "text/plain" and "attachment" not in content_disposition:
                    try:
                        payload = part.get_payload(decode=True)
                        if payload:
                            charset = part.get_content_charset() or 'utf-8'
                            body = payload.decode(charset)
                            break
                    except:
                        pass
                # 如果没有纯文本，尝试 HTML
                elif content_type == "text/html" and "attachment" not in content_disposition and not body:
                    try:
                        payload = part.get_payload(decode=True)
                        if payload:
                            charset = part.get_content_charset() or 'utf-8'
                            html = payload.decode(charset)
                            # 简单去除 HTML 标签
                            import re
                            body = re.sub(r'<[^>]+>', '', html)
                            body = body.strip()
                    except:
                        pass
        else:
            # 非多部分邮件
            try:
                payload = msg.get_payload(decode=True)
                if payload:
                    charset = msg.get_content_charset() or 'utf-8'
                    body = payload.decode(charset)
            except:
                pass

        return body.strip()

    # 类级别的发送时间追踪，避免频繁发送
    _last_send_time = 0
    _min_send_interval = 5  # 最小发送间隔（秒）

    def send(self, to: str, text: str, **kwargs) -> bool:
        if not self.smtp_enabled:
            _logger.debug("Email send skipped: SMTP is not fully configured")
            return False

        if not self._smtp_ready():
            self._log_smtp_skip()
            return False

        import time
        current_time = time.time()

        # 避免发送太频繁
        if current_time - self._last_send_time < self._min_send_interval:
            _logger.debug("Email send skipped: too frequent (interval < %ss)", self._min_send_interval)
            return False

        try:
            msg = MIMEMultipart()
            msg["From"] = self.from_addr
            msg["To"] = to
            msg["Reply-To"] = self.from_addr  # 添加 Reply-To 让 Gmail 识别为对话
            msg["Subject"] = kwargs.get("subject", "GitHub 热榜更新")

            msg.attach(MIMEText(text, "plain", "utf-8"))

            if self.smtp_port == 465:
                with smtplib.SMTP_SSL(
                    self.smtp_host,
                    self.smtp_port,
                    timeout=self.smtp_timeout,
                    context=ssl.create_default_context(),
                ) as server:
                    server.login(self.smtp_user, self.smtp_password)
                    server.send_message(msg)
            else:
                with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=self.smtp_timeout) as server:
                    server.ehlo()
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
                    server.login(self.smtp_user, self.smtp_password)
                    server.send_message(msg)

            self._reset_smtp_backoff()
            self._last_send_time = current_time
            _logger.info("Email sent successfully to %s", to)
            return True
        except smtplib.SMTPAuthenticationError as e:
            _logger.error("Email auth failed: %s", e)
            # 鉴权失败通常是配置问题，避免短时间内重复尝试
            self._smtp_error_count += 1
            self._smtp_next_retry_at = time.time() + min(300, self.smtp_backoff_max)
            return False
        except smtplib.SMTPRecipientsRefused as e:
            _logger.error("Email recipient refused: %s", e)
            return False
        except (smtplib.SMTPServerDisconnected, socket.timeout, TimeoutError, OSError, ssl.SSLError) as e:
            self._handle_smtp_connection_error(e)
            return False
        except Exception as e:
            self._handle_smtp_connection_error(e)
            return False

    def _smtp_ready(self) -> bool:
        return time.time() >= self._smtp_next_retry_at

    def _reset_smtp_backoff(self) -> None:
        self._smtp_error_count = 0
        self._smtp_next_retry_at = 0.0

    def _handle_smtp_connection_error(self, error: Exception) -> None:
        self._smtp_error_count += 1
        backoff = min(5 * (2 ** (self._smtp_error_count - 1)), self.smtp_backoff_max)
        self._smtp_next_retry_at = time.time() + backoff

        now = time.time()
        should_log = self._smtp_error_count <= 3 or (now - self._smtp_last_log_at) >= 60
        if should_log:
            self._smtp_last_log_at = now
            _logger.warning(
                "Email send failed: %s. Retrying in %ss (attempt %s)",
                error,
                backoff,
                self._smtp_error_count,
            )

    def _log_smtp_skip(self) -> None:
        now = time.time()
        if now - self._smtp_last_log_at >= 30:
            self._smtp_last_log_at = now
            wait = max(0, int(self._smtp_next_retry_at - now))
            _logger.warning("Email send skipped: SMTP in backoff (%ss remaining)", wait)
