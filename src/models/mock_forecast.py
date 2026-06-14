"""*
Mock модель прогнозирования заявок.
В production здесь вызов реальной модели градиентного бустинга.
"""

import random
from typing import Any

from src.db.connection import get_db_connection

def predict_leads(plan_rows: list[dict]) -> list[dict]:
    """
    Принимает строки плана, возвращает их же с обновлённым прогнозом заявок.
    
    Mock-логика: predicted_leads = apartments * coefficient * random_factor
    В production: вызов pickle-модели
    """
    coefficients = {
        "mailbox_flyer": 0.02,
        "door_hanger": 0.03,
        "elevator_poster": 0.025,
        "stairwell_banner": 0.015,
    }
    
    results = []
    for row in plan_rows:
        ad_type = row.get("ad_type", "mailbox_flyer")
        apartments = row.get("apartments", 100)
        frequency = row.get("frequency", 1)
        existing_subscribers = row.get("existing_subscribers", 0)
        
        base_coeff = coefficients.get(ad_type, 0.02)
        # Чем больше абонентов уже есть — тем меньше потенциал
        saturation_factor = max(0.3, 1.0 - (existing_subscribers / apartments) * 0.8)
        random_factor = random.uniform(0.8, 1.2)
        
        predicted_leads = round(
            apartments * base_coeff * frequency * saturation_factor * random_factor, 1
        )
        
        results.append({
            **row,
            "predicted_leads": predicted_leads,
        })
    
    return results


def recalculate_with_corrections(
    branch: str,
    month: str,
    corrections: list[dict],
) -> tuple[list[dict], dict[str, Any]]:
    """
    Применяет корректировки к плану и пересчитывает прогноз.
    
    Returns:
        (adjusted_plan, summary) — новый план и сводка изменений
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Текущий план
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

    if not rows:
        cursor.close()
        conn.close()
        raise ValueError(f"Не найден исходный план для {branch} за период {month}.")

    columns = [desc[0] for desc in cursor.description]
    original_plan = [dict(zip(columns, row)) for row in rows]

    cursor.close()
    conn.close()

    corrections_map = {c["house_id"]: c for c in corrections}

    adjusted_plan = []
    removed_houses = []
    added_houses = []
    modified_houses = []

    for row in original_plan:
        house_id = row["house_id"]

        if house_id in corrections_map:
            correction = corrections_map[house_id]
            action = correction.get("action", "modify")

            if action == "remove":
                removed_houses.append(house_id)
                continue
            elif action == "modify":
                modified_row = {**row}
                if "ad_type" in correction:
                    modified_row["ad_type"] = correction["ad_type"]
                if "frequency" in correction:
                    modified_row["frequency"] = correction["frequency"]
                adjusted_plan.append(modified_row)
                modified_houses.append(house_id)
            else:
                adjusted_plan.append(row)
        else:
            adjusted_plan.append(row)

    for correction in corrections:
        if correction.get("action") == "add":
            added_houses.append(correction["house_id"])
            adjusted_plan.append({
                "house_id": correction["house_id"],
                "ad_type": correction.get("ad_type", "mailbox_flyer"),
                "frequency": correction.get("frequency", 1),
                "apartments": correction.get("apartments", 100),
                "existing_subscribers": correction.get("existing_subscribers", 0),
            })

    # Пересчёт прогнозов/стоимости
    adjusted_plan = predict_leads(adjusted_plan)

    def totals(plan: list[dict]) -> tuple[float, float]:
        leads = sum(float(r.get("predicted_leads", 0) or 0) for r in plan)
        cost = sum(float(r.get("cost", 0) or 0) for r in plan)
        return leads, cost

    original_leads, original_cost = totals(original_plan)
    adjusted_leads, adjusted_cost = totals(adjusted_plan)

    summary = {
        "removed_count": len(removed_houses),
        "added_count": len(added_houses),
        "modified_count": len(modified_houses),
        "original_leads_total": round(original_leads, 1),
        "adjusted_leads_total": round(adjusted_leads, 1),
        "leads_delta": round(adjusted_leads - original_leads, 1),
        "original_cost_total": round(original_cost, 2),
        "adjusted_cost_total": round(adjusted_cost, 2),
        "cost_delta": round(adjusted_cost - original_cost, 2),
    }

    # Формируем comparison_report (короткий человекочитаемый отчёт)
    lines = []
    lines.append(f"Сравнение плана для '{branch}' на {month}:")
    lines.append(
        f"- Лиды: было {summary['original_leads_total']}, стало {summary['adjusted_leads_total']} "
        f"(дельта {summary['leads_delta']:+})"
    )
    lines.append(
        f"- Бюджет: было {summary['original_cost_total']}, стало {summary['adjusted_cost_total']} "
        f"(дельта {summary['cost_delta']:+})"
    )
    lines.append(
        f"- Изменения: добавлено {summary['added_count']}, удалено {summary['removed_count']}, изменено {summary['modified_count']}"
    )

    # Примеры изменений (первые 5 на категорию)
    def sample(lst: list[str], title: str) -> None:
        if lst:
            preview = ", ".join(map(str, lst[:5]))
            more = f" … и ещё {len(lst) - 5}" if len(lst) > 5 else ""
            lines.append(f"- {title}: {preview}{more}")

    sample(added_houses, "Добавлены дома")
    sample(removed_houses, "Удалены дома")
    sample(modified_houses, "Изменены дома")

    summary["comparison_report"] = "\n".join(lines)

    return adjusted_plan, summary