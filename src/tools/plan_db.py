"""Tool: работа с базой данных планов."""

import json
from psycopg2.extras import Json
from datetime import datetime
from typing import Literal, Dict, Any, List, Tuple, Optional

from langchain_core.tools import tool
from src.db.connection import get_db_connection

import logging

logger = logging.getLogger(__name__)

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
def get_finalize_status(month: str) -> dict:
    """
    Проверяет, финализирован ли план за указанный месяц.
    Возвращает: {"plan_finalized": bool, "plan_finalized_at": "YYYY-MM-DD HH:MM:SS" | None}
    """
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT MAX(finalized_at) AS finalized_at
            FROM plans_final
            WHERE month = %s
            """,
            (month,), # важно: кортеж из одного элемента
        )
        row = cur.fetchone()
        finalized_at = row[0] if row else None
        return {
            "plan_finalized": bool(finalized_at),
            "plan_finalized_at": finalized_at.isoformat(sep=" ") if finalized_at else None,
        }
    except Exception as e:
        logger.error(f"get_finalize_status failed: {e}")
        raise
    finally:
        cur.close()
        conn.close()


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
        

@tool
def get_corrections_status_from_log(month: str) -> dict:
    """
    Агрегирует статус корректировок по всем филиалам за указанный месяц из corrections_log.
    Input: {"month": "YYYY-MM"} 

    Output:
        {
          "month": "YYYY-MM",
          "total_branches": int,
          "branches": {
            "Новосибирск": {
              "submitted": bool,
              "status": "pending|approved|rejected|null",
              "submitted_at": "2026-06-18T10:22:33Z" | null,
              "editor_id": "u123" | null,
              "reviewed_by": "mgr42" | null,
              "reviewed_at": "2026-06-19T12:00:00Z" | null
            },
            ...
          }
        }
    """
    if not month or not isinstance(month, str):
        raise ValueError("get_corrections_status_from_log: 'month' (YYYY-MM) is required")

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        # 1) Получаем список всех филиалов.
        branches: List[str] = []
        cursor.execute("SELECT DISTINCT branch FROM users;")
        rows = cursor.fetchall()
        branches = [r[0] for r in rows] if rows else []

        # Если пусто — вернём пустую структуру
        if not branches:
            return {
                "month": month,
                "total_branches": 0,
                "branches": {},
            }

        # 2) Берём по каждому филиалу последнюю запись (по submitted_at)
        # Так как в таблице уникальность (branch, month, editor_id),
        # для одного branch может быть несколько editor'ов.
        # Возьмём последнюю по времени как актуальную для статуса.
        cursor.execute(
            """
            WITH ranked AS (
                SELECT
                    branch,
                    month,
                    editor_id,
                    corrections_json,
                    submitted_at,
                    status,
                    reviewed_by,
                    reviewed_at,
                    ROW_NUMBER() OVER (
                        PARTITION BY branch, month
                        ORDER BY submitted_at DESC
                    ) AS rn
                FROM corrections_log
                WHERE month = %s
            )
            SELECT
                branch,
                editor_id,
                corrections_json,
                submitted_at,
                status,
                reviewed_by,
                reviewed_at
            FROM ranked
            WHERE rn = 1;
            """,
            (month,),
        )
        rows = cursor.fetchall()

        # Map последних записей по филиалам
        latest_by_branch: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            b, editor_id, corrections_json, submitted_at, status, reviewed_by, reviewed_at = row
            latest_by_branch[b] = {
                "editor_id": editor_id,
                "corrections_json": corrections_json,
                "submitted_at": submitted_at,
                "status": status,
                "reviewed_by": reviewed_by,
                "reviewed_at": reviewed_at,
            }

        # 3) Сформируем ответ по каждому филиалу (включая те, у кого не было записей → “без изменений”)
        result_branches: Dict[str, Dict[str, Any]] = {}
        for b in branches:
            data = latest_by_branch.get(b)
            if data:
                submitted_at = data["submitted_at"]
                status = data["status"] or "pending"
                result_branches[b] = {
                    "submitted": True,
                    "status": status,
                    "submitted_at": _to_iso_z(submitted_at),
                    "editor_id": data.get("editor_id"),
                    "reviewed_by": data.get("reviewed_by"),
                    "reviewed_at": _to_iso_z(data.get("reviewed_at")),
                }
            else:
                # Нет записей в логе — трактуем как “без изменений”
                result_branches[b] = {
                    "submitted": False,
                    "status": None,
                    "submitted_at": None,
                    "editor_id": None,
                    "reviewed_by": None,
                    "reviewed_at": None,
                }

        return {
            "month": month,
            "total_branches": len(branches),
            "branches": result_branches,
        }

    except Exception as e:
        logger.error(f"get_corrections_status_from_log failed: {e}")
        conn.rollback()
        raise
    finally:
        try:
            conn.commit()
        except Exception:
            pass
        cursor.close()
        conn.close()
        

@tool        
def approve_corrections_for_branch(branch, month, reviewed_by) -> dict:
    """
    Утвердить корректировки для филиала за месяц: status='approved', reviewed_by/at.
    Выбирается последняя запись по submitted_at.
    """
    if not branch or not month:
        raise ValueError("approve_corrections_for_branch: 'branch' and 'month' are required")   

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            WITH last_rec AS (
              SELECT id
              FROM corrections_log
              WHERE branch = %s AND month = %s
              ORDER BY submitted_at DESC
              LIMIT 1
            )
            UPDATE corrections_log cl
            SET status = 'approved',
                reviewed_by = %s,
                reviewed_at = NOW()
            FROM last_rec
            WHERE cl.id = last_rec.id
            RETURNING cl.id;
            """,
            (branch, month, reviewed_by),
        )
        row = cur.fetchone()
        conn.commit()
        return {"success": bool(row), "branch": branch, "month": month}
    except Exception as e:
        conn.rollback()
        logger.error(f"approve_corrections_for_branch failed: {e}")
        raise
    finally:
        cur.close()
        conn.close()
        

@tool       
def reject_corrections_for_branch(branch, month, reviewed_by, reason) -> dict:
    """
    Отклонить корректировки для филиала за месяц: status='rejected', reviewed_by/at.
    При наличии reason — сохраняем в corrections_json->'meta'.
    Если нет meta — добавляем.
    """
    if not branch or not month:
        raise ValueError("reject_corrections_for_branch: 'branch' and 'month' are required")      

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # 1) Найти последнюю запись
        cur.execute(
            """
            SELECT id, corrections_json
            FROM corrections_log
            WHERE branch = %s AND month = %s
            ORDER BY submitted_at DESC
            LIMIT 1
            """,
            (branch, month),
        )
        row = cur.fetchone()
        if not row:
            conn.commit()
            return {"success": False, "branch": branch, "month": month, "message": "no submission found"}

        rec_id, cj = row

        # 2) Нормализация JSON к dict
        if cj is None:
            cj = {}
        elif isinstance(cj, str):
            import json as _json
            try:
                cj = _json.loads(cj)
            except Exception:
                cj = {}
        if not isinstance(cj, dict):
            cj = {"data": cj}

        # 3) Применить причину (если есть)
        reason_norm = (reason or "").strip()
        if reason_norm:
            meta = cj.get("meta") or {}
            meta["rejection_reason"] = reason_norm
            cj["meta"] = meta

        # 4) Обновить запись
        import json as _json
        cj_text = _json.dumps(cj, ensure_ascii=False)
        cur.execute(
            """
            UPDATE corrections_log
            SET status = 'rejected',
                reviewed_by = %s,
                reviewed_at = NOW(),
                corrections_json = %s::jsonb
            WHERE id = %s
            RETURNING id
            """,
            (reviewed_by, cj_text, rec_id),
        )

        upd = cur.fetchone()
        conn.commit()
        return {
            "success": bool(upd),
            "branch": branch,
            "month": month,
            "message": None if upd else "update failed",
        }
    except Exception as e:
        conn.rollback()
        logger.error(f"reject_corrections_for_branch failed: {e}")
        raise
    finally:
        cur.close()
        conn.close()
        
 
@tool 
def finalize_month_plan(month) -> dict:
    """
    Финализировать план за месяц:
    - Берём рабочие планы из plans (branch, month, house_id, ad_type, ...).
    - Применяем approved-патчи из corrections_log (по последней записи на филиал).
    - Полностью перезаписываем plans_final для месяца.
    """
    if not month:
        raise ValueError("finalize_month_plan: 'month' is required")  

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # 1) Список филиалов из houses/plans/log
        cur.execute(
            """
            SELECT DISTINCT branch FROM houses
            UNION
            SELECT DISTINCT branch FROM plans WHERE month = %s
            UNION
            SELECT DISTINCT branch FROM corrections_log WHERE month = %s
            ORDER BY 1;
            """,
            (month, month),
        )
        branches = [r[0] for r in cur.fetchall()] or []
        if not branches:
            return {"success": True, "month": month, "finalized_rows": 0, "finalized_branches": 0}

        # 2) Загружаем рабочие планы за месяц
        cur.execute(
            """
            SELECT branch, house_id, ad_type, frequency, apartments, existing_subscribers, predicted_leads, cost
            FROM plans
            WHERE month = %s;
            """,
            (month,),
        )
        base_rows_by_branch: Dict[str, List[Dict[str, Any]]] = {b: [] for b in branches}
        for b, house_id, ad_type, frequency, apartments, existing_subscribers, predicted_leads, cost in cur.fetchall():
            base_rows_by_branch[b].append({
                "house_id": house_id,
                "ad_type": ad_type,
                "frequency": frequency,
                "apartments": apartments,
                "existing_subscribers": existing_subscribers,
                "predicted_leads": predicted_leads,
                "cost": cost,
            })

        # 3) Последние записи логов по филиалам
        cur.execute(
            """
            WITH ranked AS (
                SELECT
                    branch,
                    corrections_json,
                    status,
                    submitted_at,
                    ROW_NUMBER() OVER (PARTITION BY branch, month ORDER BY submitted_at DESC) rn
                FROM corrections_log
                WHERE month = %s
            )
            SELECT branch, corrections_json, status
            FROM ranked
            WHERE rn = 1;
            """,
            (month,),
        )
        last_log: Dict[str, Dict[str, Any]] = {r[0]: {"corrections_json": r[1], "status": r[2]} for r in cur.fetchall()}

        # 4) Собираем финальные строки
        final_rows: List[Tuple[str, str, str, str, int, Optional[int], Optional[int], Optional[float], Optional[float]]] = []

        for b in branches:
            base_rows = base_rows_by_branch.get(b, [])
            approved_patch = None
            entry = last_log.get(b)
            if entry and entry.get("status") == "approved":
                approved_patch = entry.get("corrections_json")

            merged_rows = _apply_patch_to_plan_rows(base_rows, approved_patch)

            for row in merged_rows:
                final_rows.append((
                    b,
                    month,
                    str(row.get("house_id") or ""),
                    str(row.get("ad_type") or ""),
                    int(row.get("frequency") or 1),
                    _to_int(row.get("apartments")),
                    _to_int(row.get("existing_subscribers")),
                    _to_float(row.get("predicted_leads")),
                    _to_float(row.get("cost")),
                ))

        # 5) Перезаписываем plans_final за месяц
        cur.execute("DELETE FROM plans_final WHERE month = %s;", (month,))
        if final_rows:
            args_str = ",".join(
                cur.mogrify("(%s,%s,%s,%s,%s,%s,%s,%s,%s)", r).decode("utf-8")
                for r in final_rows
            )
            cur.execute(
                f"""
                INSERT INTO plans_final
                (branch, month, house_id, ad_type, frequency, apartments, existing_subscribers, predicted_leads, cost)
                VALUES {args_str};
                """
            )

        conn.commit()
        return {
            "success": True,
            "month": month,
            "finalized_rows": len(final_rows),
            "finalized_branches": len(branches),
        }

    except Exception as e:
        conn.rollback()
        logger.error(f"finalize_month_plan failed: {e}")
        raise
    finally:
        cur.close()
        conn.close()
        
        
# ============================================================
# HELPERS
# ============================================================
def _apply_patch_to_plan_rows(base_rows: List[Dict[str, Any]], patch: Any) -> List[Dict[str, Any]]:
    """
    Применяет approved-патч к списку строк:
    - Если patch — список, считаем это полной заменой плана филиала.
    - Если patch — dict с ключом 'full_plan' (list) → полная замена.
    - Если patch — dict с ключом 'updates' (list) → поверхностно обновляем/добавляем по ключу (house_id, ad_type).
    - Иначе возвращаем base_rows.
    """
    if not patch:
        return base_rows or []
        
    if isinstance(patch, list):
        return patch

    if isinstance(patch, dict):
        if isinstance(patch.get("full_plan"), list):
            return patch["full_plan"]

        updates = patch.get("updates")
        if isinstance(updates, list):
            index: Dict[Tuple[str, str], Dict[str, Any]] = {}
            for r in base_rows or []:
                key = (str(r.get("house_id") or ""), str(r.get("ad_type") or ""))
                index[key] = dict(r)

            for upd in updates:
                key = (str(upd.get("house_id") or ""), str(upd.get("ad_type") or ""))
                if key in index:
                    # обновляем поля, кроме ключей
                    index[key].update({k: v for k, v in upd.items() if k not in ("house_id", "ad_type")})
                else:
                    index[key] = dict(upd)

            return list(index.values())

    return base_rows or []        
                
def _to_iso_z(dt: Any) -> Optional[str]:
    if not dt:
        return None
    if isinstance(dt, datetime):
        return dt.replace(tzinfo=None).isoformat(timespec="seconds") + "Z"
    try:
        return str(dt)
    except Exception:
        return None

def _to_int(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except Exception:
        return None

def _to_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except Exception:
        return None        