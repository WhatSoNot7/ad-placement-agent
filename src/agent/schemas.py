"""Pydantic-схемы для structured output агента."""

from pydantic import BaseModel, Field, field_validator
from typing import Optional
from enum import Enum


# === Схема для классификации намерения ===

class UserIntent(str, Enum):
    GET_PLAN = "get_plan"
    SUBMIT_CORRECTIONS = "submit_corrections"
    APPROVE_PLAN = "approve_plan"
    ASK_STATUS = "ask_status"
    UNCLEAR = "unclear"


class IntentClassification(BaseModel):
    """Результат классификации намерения пользователя."""
    
    intent: UserIntent = Field(..., description="Определённое намерение пользователя")
    target_month: Optional[str] = Field(
        None,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
        description="Целевой месяц в формате YYYY-MM. Обязателен для get_plan и submit_corrections. Для остальных интентов — null."
    )
    reasoning: str = Field(..., description="Краткое обоснование выбора намерения")

    @field_validator("target_month", mode="before")
    @classmethod
    def parse_null_string(cls, v):
        """Конвертировать строку 'null' в None."""
        if isinstance(v, str) and v.lower() in ("null", "none", ""):
            return None
        return v


# === Схема для ответа пользователю ===

class ActionStatus(str, Enum):
    SUCCESS = "success"
    ERROR = "error"
    PARTIAL = "partial"
    NEEDS_CLARIFICATION = "needs_clarification"


class AgentResponse(BaseModel):
    """Основная схема ответа агента пользователю."""
    
    message: str = Field(
        ..., 
        min_length=1,
        description="Текст ответа пользователю на русском языке"
    )
    status: ActionStatus = Field(..., description="Статус выполнения действия")
    action_performed: Optional[str] = Field(
        None, 
        description="Какое действие было выполнено (get_plan, validate_file, и т.д.)"
    )
    data_summary: Optional[str] = Field(
        None,
        description="Краткая сводка по данным, если были получены/обработаны"
    )
    next_steps: list[str] = Field(
        default_factory=list,
        description="Рекомендуемые следующие шаги для пользователя"
    )
    requires_user_action: bool = Field(
        default=False,
        description="Требуется ли действие от пользователя для продолжения"
    )


# === Схема для валидации корректировок ===

class ValidationSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class ValidationIssue(BaseModel):
    """Одна проблема, найденная при валидации файла корректировок."""
    
    severity: ValidationSeverity = Field(..., description="Серьёзность проблемы")
    row: Optional[int] = Field(None, ge=1, description="Номер строки с ошибкой")
    column: Optional[str] = Field(None, description="Название столбца с ошибкой")
    description: str = Field(..., description="Описание проблемы")


class CorrectionValidationResult(BaseModel):
    """Результат валидации файла корректировок."""
    
    is_valid: bool = Field(..., description="Прошёл ли файл валидацию без критических ошибок")
    total_rows: int = Field(..., ge=0, description="Общее количество строк в файле")
    issues: list[ValidationIssue] = Field(
        default_factory=list,
        description="Найденные проблемы"
    )
    summary: str = Field(..., description="Краткое резюме валидации")


# === Схема для graceful degradation ===

class ErrorResponse(BaseModel):
    """Схема ответа при невозможности обработать запрос."""
    
    success: bool = Field(default=False)
    message: str = Field(
        default="К сожалению, в данный момент я не могу обработать ваш запрос. "
                "Команда разработки уже уведомлена о проблеме."
    )
    request_id: str = Field(..., description="ID запроса для отслеживания")
    retry_after_seconds: Optional[int] = Field(
        default=60, 
        description="Рекомендуемое время до повторной попытки"
    )