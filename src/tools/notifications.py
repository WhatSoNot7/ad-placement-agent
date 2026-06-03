"""Tool: отправка уведомлений."""

import logging
from datetime import datetime

from langchain_core.tools import tool

logger = logging.getLogger(__name__)


# В production здесь будет реальный API мессенджера
NOTIFICATION_LOG: list[dict] = []


@tool
def send_notification(recipient_id: str, message: str, notification_type: str = "info") -> dict:
    """
    Отправляет уведомление сотруднику.
    
    В MVP: логирует сообщение.
    В production: отправляет в корпоративный мессенджер/email.
    
    Args:
        recipient_id: ID получателя
        message: Текст сообщения
        notification_type: Тип (info/warning/action_required)
    
    Returns:
        {"success": bool, "message": str}
    """
    notification = {
        "recipient_id": recipient_id,
        "message": message,
        "type": notification_type,
        "sent_at": datetime.now().isoformat(),
    }
    
    NOTIFICATION_LOG.append(notification)
    logger.info(f"[NOTIFICATION → {recipient_id}] ({notification_type}): {message}")
    
    return {
        "success": True,
        "message": f"Уведомление отправлено: {recipient_id}",
    }