"""Tool: работа с базой данных планов."""

import json
from datetime import datetime
from typing import Literal

from langchain_core.tools import tool
from src.db.connection import get_db_connection


@tool
def query_plan_db(branch: str, month: str) -> dict:
    """
    Проверяет наличие плана и выгружает данные.
    
    Args:
        branch: Название филиала (например, "Новосибирск")
        month: Месяц в формате YYYY-MM (например, "2025-07")
    
    Returns:
        {"status": "exists"|"not_ready"|"not_found", "data": [...], "message": "..."}
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Проверяем наличие плана
    cursor.execute(
        """
        SELECT house_id, ad_type, frequency, apartments, 
               existing_subscribers, predicted_leads, cost
        FROM plans 
        WHERE branch = %s AND month = %s
        ORDER BY house_id
        """,
        (branch, month),
    )
    rows = cursor.fetchall()
    
    if rows:
        columns = [desc[0] for desc in cursor.description]
        data = [dict(zip(columns, row)) for row in rows]
        conn.close()
        return {
            "status": "exists",
            "data": data,
            "message": f"План на {month} по филиалу {branch} найден. {len(data)} домов в плане.",
        }
    
    # Плана нет — определяем причину
    # Парсим целевой месяц
    target_date = datetime.strptime(month, "%Y-%m")
    now = datetime.now()
    
    # Если целевой месяц далеко в будущем — слишком рано
    # План обычно формируется за 10 дней до начала месяца
    plan_expected_date = target_date.replace(day=1) - __import__("datetime").timedelta(days=10)
    
    conn.close()
    
    if now < plan_expected_date:
        return {
            "status": "not_ready",
            "data": [],
            "message": (
                f"План на {month} по филиалу {branch} ещё не сформирован. "
                f"Ожидаемая дата формирования: {plan_expected_date.strftime('%d.%m.%Y')}."
            ),
        }
    else:
        return {
            "status": "not_found",
            "data": [],
            "message": (
                f"План на {month} по филиалу {branch} не найден, хотя должен быть готов. "
                f"Возможно, произошла задержка. Могу уведомить автора модели."
            ),
        }


@tool
def save_corrections_to_db(branch: str, month: str, editor_id: str, corrections: list[dict]) -> dict:
    """
    Сохраняет валидные корректировки в БД.
    
    Args:
        branch: Филиал
        month: Месяц
        editor_id: ID сотрудника, приславшего корректировки
        corrections: Список корректировок
    
    Returns:
        {"success": bool, "message": str}
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute(
            """
            INSERT INTO corrections_log (branch, month, editor_id, corrections_json, submitted_at, status)
            VALUES (%s, %s, %s, %s, NOW(), 'pending')
            ON CONFLICT (branch, month, editor_id) 
            DO UPDATE SET corrections_json = EXCLUDED.corrections_json,
                         submitted_at = NOW(),
                         status = 'pending'
            """,
            (branch, month, editor_id, json.dumps(corrections, ensure_ascii=False)),
        )
        conn.commit()
        conn.close()
        return {"success": True, "message": f"Корректировки сохранены: {len(corrections)} изменений."}
    except Exception as e:
        conn.rollback()
        conn.close()
        return {"success": False, "message": f"Ошибка сохранения: {str(e)}"}


@tool
def save_final_plan(branch: str, month: str, plan_data: list[dict]) -> dict:
    """
    Сохраняет финализированный план в БД.
    
    Args:
        branch: Филиал
        month: Месяц
        plan_data: Итоговый план
    
    Returns:
        {"success": bool, "message": str}
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        # Удаляем старый план
        cursor.execute(
            "DELETE FROM plans_final WHERE branch = %s AND month = %s",
            (branch, month),
        )
        
        # Вставляем новый
        for row in plan_data:
            cursor.execute(
                """
                INSERT INTO plans_final (branch, month, house_id, ad_type, frequency, 
                                         apartments, existing_subscribers, predicted_leads, cost)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (branch, month, row["house_id"], row["ad_type"], row["frequency"],
                 row["apartments"], row["existing_subscribers"], row["predicted_leads"], row["cost"]),
            )
        
        conn.commit()
        conn.close()
        return {"success": True, "message": f"Финальный план сохранён: {len(plan_data)} домов."}
    except Exception as e:
        conn.rollback()
        conn.close()
        return {"success": False, "message": f"Ошибка: {str(e)}"}