"""邮件通道 (s04) - 支持 Gmail IMAP 接收和 SMTP 发送"""
import os
import smtplib
import imaplib
import email
import time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import decode_header
from typing import Optional, List
from .base import Channel, InboundMessage


class EmailChannel(Channel):
    name = "email"

    def __init__(self):
        # SMTP 发邮件配置
        self.smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
        self.smtp_port = int(os.getenv("SMTP_PORT", "587"))
        self.smtp_user = os.getenv("SMTP_USER")
        self.smtp_password = os.getenv("SMTP_PASSWORD")
        self.from_addr = os.getenv("SMTP_FROM", self.smtp_user)

        # IMAP 收邮件配置 (Gmail)
        self.imap_host = os.getenv("IMAP_HOST", "imap.gmail.com")
        self.imap_port = int(os.getenv("IMAP_PORT", "993"))
        self.imap_user = os.getenv("IMAP_USER")
        self.imap_password = os.getenv("IMAP_PASSWORD")

        # 追踪已处理的邮件，避免重复处理
        # 使用有界集合避免内存泄漏，最多保留 1000 个 ID
        from collections import deque
        self._processed_ids: deque = deque(maxlen=1000)

    def receive(self) -> Optional[InboundMessage]:
        """通过 IMAP 轮询接收邮件"""
        if not self.imap_user or not self.imap_password:
            return None

        try:
            return self._receive_imap()
        except Exception as e:
            print(f"IMAP receive error: {e}")
            return None

    def receive_all(self) -> List[InboundMessage]:
        """接收所有未处理的新邮件"""
        if not self.imap_user or not self.imap_password:
            return []

        try:
            return self._receive_all_imap()
        except Exception as e:
            print(f"IMAP receive all error: {e}")
            return []

    def _receive_imap(self) -> Optional[InboundMessage]:
        """使用 IMAP 接收单封最新邮件"""
        mail = imaplib.IMAP4_SSL(self.imap_host, self.imap_port)
        mail.login(self.imap_user, self.imap_password)
        mail.select("INBOX")

        # 搜索未读邮件
        typ, data = mail.search(None, 'UNSEEN')
        mail_ids = data[0].split()

        if not mail_ids:
            mail.logout()
            return None

        # 取最新的一封
        latest_id = mail_ids[-1]
        msg = self._fetch_mail(mail, latest_id)

        # 标记为已读
        mail.store(latest_id, '+FLAGS', '\\Seen')
        mail.logout()

        return msg

    def _receive_all_imap(self) -> List[InboundMessage]:
        """接收所有未读邮件"""
        messages = []

        mail = imaplib.IMAP4_SSL(self.imap_host, self.imap_port)
        mail.login(self.imap_user, self.imap_password)
        mail.select("INBOX")

        # 搜索未读邮件
        typ, data = mail.search(None, 'UNSEEN')
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

        mail.logout()
        return messages

    def _fetch_mail(self, mail, mail_id) -> Optional[InboundMessage]:
        """从 IMAP 获取邮件内容"""
        typ, msg_data = mail.fetch(mail_id, '(RFC822)')

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
                        subject = subject.decode(encoding or 'utf-8')
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
        if not self.smtp_host:
            print("Email send skipped: no SMTP_HOST configured")
            return False

        import time
        current_time = time.time()

        # 避免发送太频繁
        if current_time - self._last_send_time < self._min_send_interval:
            print(f"Email send skipped: too frequent (interval < {self._min_send_interval}s)")
            return False

        try:
            msg = MIMEMultipart()
            msg["From"] = self.from_addr
            msg["To"] = to
            msg["Reply-To"] = self.from_addr  # 添加 Reply-To 让 Gmail 识别为对话
            msg["Subject"] = kwargs.get("subject", "GitHub 热榜更新")

            msg.attach(MIMEText(text, "plain", "utf-8"))

            with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                server.starttls()
                server.login(self.smtp_user, self.smtp_password)
                server.send_message(msg)

            self._last_send_time = current_time
            print(f"Email sent successfully to {to}")
            return True
        except smtplib.SMTPAuthenticationError as e:
            print(f"Email auth failed: {e}")
            return False
        except smtplib.SMTPRecipientsRefused as e:
            print(f"Email recipient refused: {e}")
            return False
        except Exception as e:
            print(f"Email send failed: {e}")
            return False
