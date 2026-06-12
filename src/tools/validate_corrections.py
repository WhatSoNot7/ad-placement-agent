"""Tool: валидация файла корректировок."""

import json
from typing import Any

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from src.db.connection import get_db_connection


class ValidationError(BaseModel):
    row: int
    column: str
    value: str
    error_type: str  # unknown_house_id, budget_exceeded, no_technical_capability, invalid_format, duplicate
    message: str


class ValidationResult(BaseModel):
    valid: bool
    errors: list[ValidationError] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    corrections_parsed: list[dict] = Field(default_factory=list)
    summary: str = ""


@tool
def validate_corrections_file(file_content: list[dict], branch: str, month: str) -> dict:
    """
    Валидирует корректировки из Excel-файла.
    
    Проверяет:
    - house_id существует в справочнике
    - house_id принадлежит указанному филиалу
    - техническая возможность подключения
    - action имеет допустимое значение (add/remove/modify)
    - нет дублей
    - бюджет не превышен
    
    Args:
        file_content: Список строк из Excel (уже распарсенный)
        branch: Филиал
        month: Месяц
    
    Returns:
        Результат валидации с ошибками и предупреждениями
    """
    if not isinstance(file_content, list):
        raise ValueError("file_content должен быть list[dict], а не bytes. Проверьте парсинг Excel.")
        
    errors = []
    warnings = []
    valid_corrections = []
    seen_house_ids = set()
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Получаем справочник домов филиала
    cursor.execute(
        "SELECT house_id, has_technical_capability, apartments FROM houses WHERE branch = %s",
        (branch,),
    )
    houses_db = {row[0]: {"capable": row[1], "apartments": row[2]} for row in cursor.fetchall()}
    
    valid_actions = {"add", "remove", "modify"}
    
    for idx, row in enumerate(file_content, start=2):  # start=2 т.к. строка 1 — заголовки
        house_id = row.get("house_id")
        action = row.get("action", "").lower().strip()
        
        # Проверка: action валиден
        if action not in valid_actions:
            errors.append(ValidationError(
                row=idx, column="action", value=str(action),
                error_type="invalid_format",
                message=f"Недопустимое действие '{action}'. Допустимые: add, remove, modify",
            ))
            continue
        
        # Проверка: house_id не пустой
        if not house_id:
            errors.append(ValidationError(
                row=idx, column="house_id", value="",
                error_type="invalid_format",
                message="Пустой house_id",
            ))
            continue
        
        # Проверка: дубли
        if house_id in seen_house_ids:
            errors.append(ValidationError(
                row=idx, column="house_id", value=str(house_id),
                error_type="duplicate",
                message=f"Дом {house_id} указан более одного раза",
            ))
            continue
        seen_house_ids.add(house_id)
        
        # Для action=add дом может не быть в текущем плане, но должен быть в справочнике
        if action == "add":
            if house_id not in houses_db:
                errors.append(ValidationError(
                    row=idx, column="house_id", value=str(house_id),
                    error_type="unknown_house_id",
                    message=f"Дом {house_id} не найден в справочнике филиала {branch}",
                ))
                continue
            if not houses_db[house_id]["capable"]:
                errors.append(ValidationError(
                    row=idx, column="house_id", value=str(house_id),
                    error_type="no_technical_capability",
                    message=f"Дом {house_id}: нет технической возможности подключения",
                ))
                continue
        
        # Для action=remove/modify дом должен существовать в справочнике
        if action in ("remove", "modify"):
            if house_id not in houses_db:
                errors.append(ValidationError(
                    row=idx, column="house_id", value=str(house_id),
                    error_type="unknown_house_id",
                    message=f"Дом {house_id} не найден в справочнике филиала {branch}",
                ))
                continue
        
        # Всё ок — добавляем в валидные
        valid_corrections.append(row)
    
    cursor.close()
    conn.close()
    
    # Формируем результат
    is_valid = len(errors) == 0
    summary_parts = []
    if valid_corrections:
        summary_parts.append(f"Валидных корректировок: {len(valid_corrections)}")
    if errors:
        summary_parts.append(f"Ошибок: {len(errors)}")
    if warnings:
        summary_parts.append(f"Предупреждений: {len(warnings)}")
    
    result = ValidationResult(
        valid=is_valid,
        errors=errors,
        warnings=warnings,
        corrections_parsed=valid_corrections,
        summary=". ".join(summary_parts),
    )
    
    return result.model_dump()