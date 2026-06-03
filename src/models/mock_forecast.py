"""
Mock модель прогнозирования заявок.
В production здесь вызов реальной модели градиентного бустинга.
"""

import random
from typing import Any


def predict_leads(plan_rows: list[dict]) -> list[dict]:
    """
    Принимает строки плана, возвращает их же с обновлённым прогнозом заявок.
    
    Mock-логика: predicted_leads = apartments * coefficient * random_factor
    В production: вызов pickle-модели или API endpoint.
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
    original_plan: list[dict],
    corrections: list[dict],
) -> tuple[list[dict], dict[str, Any]]:
    """
    Применяет корректировки к плану и пересчитывает прогноз.
    
    Returns:
        (adjusted_plan, summary) — новый план и сводка изменений
    """
    # Индексируем корректировки по house_id
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
                continue  # Исключаем из плана
            elif action == "modify":
                # Применяем изменения
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
    
    # Добавляем новые дома из корректировок
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
    
    # Пересчитываем прогноз
    adjusted_plan = predict_leads(adjusted_plan)
    
    # Формируем сводку
    original_leads = sum(r.get("predicted_leads", 0) for r in original_plan)
    adjusted_leads = sum(r.get("predicted_leads", 0) for r in adjusted_plan)
    original_cost = sum(r.get("cost", 0) for r in original_plan)
    adjusted_cost = sum(r.get("cost", 0) for r in adjusted_plan)
    
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
    
    return adjusted_plan, summary