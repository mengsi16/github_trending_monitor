"""邮件发送工具 (s04, s08)"""
import smtplib
import os
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

_logger = logging.getLogger("email_tool")

class EmailSender:
    def __init__(self):
        self.host = os.getenv("SMTP_HOST")
        self.port = int(os.getenv("SMTP_PORT", "587"))
        self.user = os.getenv("SMTP_USER")
        self.password = os.getenv("SMTP_PASSWORD")
        self.from_addr = os.getenv("SMTP_FROM", self.user)

    def send(self, to: str, subject: str, body: str) -> bool:
        """发送邮件"""
        if not self.host or not self.user:
            _logger.debug("SMTP not configured, skipping email")
            return False

        try:
            msg = MIMEMultipart()
            msg["From"] = self.from_addr
            msg["To"] = to
            msg["Subject"] = subject

            msg.attach(MIMEText(body, "plain", "utf-8"))

            with smtplib.SMTP(self.host, self.port) as server:
                server.starttls()
                server.login(self.user, self.password)
                server.send_message(msg)

            return True
        except Exception as e:
            _logger.error("Email send failed: %s", e)
            return False

    def send_to_team(self, team_id: str, subject: str, body: str) -> bool:
        """发送给团队配置的邮箱"""
        from src.config import config
        team = next((t for t in config.teams if t.id == team_id), None)
        if not team or not team.email:
            return False
        return self.send(team.email, subject, body)

def tool_email_send(to: str, subject: str, body: str) -> str:
    """发送邮件工具"""
    sender = EmailSender()
    ok = sender.send(to, subject, body)
    return "邮件发送成功" if ok else "邮件发送失败"

EMAIL_TOOLS = [
    {
        "name": "email_send",
        "description": "发送邮件",
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "收件人邮箱"},
                "subject": {"type": "string", "description": "邮件主题"},
                "body": {"type": "string", "description": "邮件正文"}
            },
            "required": ["to", "subject", "body"]
        }
    }
]

EMAIL_HANDLERS = {
    "email_send": tool_email_send,
}
