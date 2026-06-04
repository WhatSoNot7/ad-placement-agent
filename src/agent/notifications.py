"""Уведомления разработчика об ошибках агента."""

import logging
import smtplib
import asyncio
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


async def send_error_notification(
    developer_email: str,
    request_id: str,
    operation: str,
    error: Optional[Exception],
    messages_dump: str,
    traceback_str: str,
) -> None:
    """
    Отправить email-уведомление разработчику об ошибке.

    Запускается в отдельном потоке, чтобы не блокировать event loop.
    """

    subject = f"[AD-PLACEMENT-AGENT] Ошибка: {operation} | {request_id}"

    body = f"""
⚠️ Агент не смог обработать запрос после всех retry.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📋 Детали:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• Request ID: {request_id}
• Операция: {operation}
• Время (UTC): {datetime.now(timezone.utc).isoformat()}
• Ошибка: {type(error).__name__ if error else "Unknown"}: {error}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📨 Входные сообщения:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{messages_dump[:2000]}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🔍 Traceback:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{traceback_str[:3000]}
"""

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _send_email_sync, developer_email, subject, body)


def _send_email_sync(to_email: str, subject: str, body: str) -> None:
    """Синхронная отправка email через SMTP."""

    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_password = os.getenv("SMTP_PASSWORD", "")
    from_email = os.getenv("SMTP_FROM", smtp_user)

    if not smtp_user or not smtp_password:
        logger.warning(
            "SMTP credentials не настроены. Email-уведомление не отправлено. "
            "Установите SMTP_USER и SMTP_PASSWORD."
        )
        # Fallback: логируем ошибку в файл, если email не настроен
        logger.error(f"[NOTIFICATION FALLBACK] To: {to_email} | Subject: {subject}\n{body}")
        return

    msg = MIMEMultipart()
    msg["From"] = from_email
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.send_message(msg)
        logger.info(f"Уведомление об ошибке отправлено на {to_email}")
    except smtplib.SMTPAuthenticationError as e:
        logger.error(f"Ошибка аутентификации SMTP: {e}")
    except smtplib.SMTPConnectError as e:
        logger.error(f"Не удалось подключиться к SMTP серверу: {e}")
    except smtplib.SMTPException as e:
        logger.error(f"Ошибка SMTP при отправке уведомления: {e}")
    except OSError as e:
        logger.error(f"Сетевая ошибка при отправке email: {e}")