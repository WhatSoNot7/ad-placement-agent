"""Pydantic-схемы для structured output агента."""

from datetime import datetime, timezone
from enum import Enum
from typing import Optional
import re

from pydantic import BaseModel, Field, field_validator, model_validator

RU_MONTHS = {
    # январь
    "январь": 1, "янв": 1,
    # февраль
    "февраль": 2, "фев": 2,
    # март
    "март": 3, "мар": 3,
    # апрель
    "апрель": 4, "апр": 4,
    # май
    "май": 5, "мая": 5, # часто встречается в родительном падеже
    # июнь
    "июнь": 6, "июн": 6,
    # июль
    "июль": 7, "июл": 7,
    # август
    "август": 8, "авг": 8,
    # сентябрь
    "сентябрь": 9, "сен": 9, "сент": 9,
    # октябрь
    "октябрь": 10, "окт": 10,
    # ноябрь
    "ноябрь": 11, "ноя": 11, "нояб": 11,
    # декабрь
    "декабрь": 12, "дек": 12,
    }

RELATIVE_PATTERNS = [
    # порядок важен: сначала “в следующем месяце”, потом “следующий месяц”
    (re.compile(r"\bв\s+следующ(ем|ий)\s+месяц(е)?\b", re.IGNORECASE), 1),
    (re.compile(r"\bследующ(ий|ем)\s+месяц(е)?\b", re.IGNORECASE), 1),
    (re.compile(r"\bв\s+прошл(ом|ый)\s+месяц(е)?\b", re.IGNORECASE), -1),
    (re.compile(r"\bпрошл(ый|ом)\s+месяц(е)?\b", re.IGNORECASE), -1),
    (re.compile(r"\bв\s+эт(ом|от)\s+месяц(е)?\b", re.IGNORECASE), 0),
    (re.compile(r"\bэт(от|ом)\s+месяц(е)?\b", re.IGNORECASE), 0),
    ]

def shift_year_month(year: int, month: int, delta_months: int) -> tuple[int, int]:
    idx = (year * 12 + (month - 1)) + delta_months
    y = idx // 12
    m = idx % 12 + 1
    return y, m


# === Схема для классификации намерения ===

class UserIntent(str, Enum):
    GET_PLAN = "get_plan"
    SUBMIT_CORRECTIONS = "submit_corrections"
    APPROVE_PLAN = "approve_plan"
    ASK_STATUS = "ask_status"
    UNCLEAR = "unclear"


class IntentClassification(BaseModel):
    """Результат классификации намерения пользователя."""
    intent: UserIntent = Field(
        ...,
        description="Определённое намерение пользователя",
    )
    target_month: Optional[str] = Field(
        None,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
        description=(
            "Целевой месяц в формате YYYY-MM. Обязателен для get_plan и "
            "submit_corrections. Для остальных интентов — null."
        ),
    )
    reasoning: str = Field(
        ...,
        description="Краткое обоснование выбора намерения",
    )

    @field_validator("target_month", mode="before")
    @classmethod
    def normalize_target_month(cls, v):
        """
        Нормализация входного значения:
        - 'null'/'None'/'' -> None
        - русские месяцы (полные/краткие), например: 'июль', 'сен', 'мая'
        - относительные выражения: 'в следующем месяце', 'следующий месяц',
          'в прошлом месяце', 'этот месяц'
        - 'MM' или 'M' -> 'YYYY-MM' с текущим годом
        - 'YYYY-M'/'YYYY-MM' -> нормализация к 'YYYY-MM'
        """
        if v is None:
            return None

        if isinstance(v, str):
            s = v.strip().lower()

            # Пустые/нулевые значения
            if s in ("null", "none", ""):
                return None

            now = datetime.now(timezone.utc)
            cy, cm = now.year, now.month

            # Относительные выражения
            for pattern, delta in RELATIVE_PATTERNS:
                if pattern.search(s):
                    y, m = shift_year_month(cy, cm, delta)
                    return f"{y:04d}-{m:02d}"

            # Русские месяцы (включая краткие и “мая”)
            if s in RU_MONTHS:
                month = RU_MONTHS[s]
                return f"{cy:04d}-{month:02d}"

            # Только месяц в цифрах, допускаем 1..12 и 01..12
            m_only = re.fullmatch(r"(0?[1-9]|1[0-2])", s)
            if m_only:
                month = int(m_only.group(1))
                return f"{cy:04d}-{month:02d}"

            # Формат "YYYY-M" или "YYYY-MM"
            ym = re.fullmatch(r"(\d{4})-(\d{1,2})", s)
            if ym:
                y = int(ym.group(1))
                m = int(ym.group(2))
                if 1 <= m <= 12:
                    return f"{y:04d}-{m:02d}"

        # Возвращаем как есть — дальше ensure_format проверит
        return v

    @field_validator("target_month")
    @classmethod
    def ensure_format(cls, v):
        """
        Строгая проверка итогового формата.
        Допускаем None, иначе только 'YYYY-MM' с 01..12.
        """
        if v is None:
            return v
        if not re.fullmatch(r"^\d{4}-(0[1-9]|1[0-2])$", v):
            raise ValueError("target_month must be in format YYYY-MM")
        return v

    # target_month обязательный для двух интентов.
    @model_validator(mode="after")
    def enforce_requirements(self):
        if self.intent in {UserIntent.GET_PLAN, UserIntent.SUBMIT_CORRECTIONS} and self.target_month is None:
            raise ValueError("target_month is required for get_plan and submit_corrections")
        return self


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