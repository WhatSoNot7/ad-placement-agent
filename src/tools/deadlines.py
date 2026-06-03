"""Tool: информация о дедлайнах."""

from datetime import datetime, timedelta

from langchain_core.tools import tool

from src.db.connection import get_db_connection


@tool
def get_deadline_info(month: str, branch: str) -> dict:
    """
    Возвращает информацию о дедлайне корректировок.
    
    Args:
        month: Месяц в формате YYYY-MM
        branch: Филиал
    
    Returns:
        {"deadline": str, "days_left": int, "is_passed": bool}
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute(
        "SELECT deadline_date FROM deadlines WHERE month = %s AND branch = %s",
        (month, branch),
    )
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    
    if row:
        deadline = row[0]
    else:
        # Дефолтный дедлайн: 25 число предыдущего месяца
        year, mon = map(int, month.split("-"))
        if mon == 1:
            deadline = datetime(year - 1, 12, 25)
        else:
            deadline = datetime(year, mon - 1, 25)
    
    now = datetime.now()
    
    if isinstance(deadline, str):
        deadline = datetime.strptime(deadline, "%Y-%m-%d")
    
    days_left = (deadline - now).days
    is_passed = now > deadline
    
    return {
        "deadline": deadline.strftime("%Y-%m-%d"),
        "deadline_display": deadline.strftime("%d.%m.%Y"),
        "days_left": max(days_left, 0),
        "is_passed": is_passed,
    }