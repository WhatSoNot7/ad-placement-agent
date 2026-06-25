"""Определение состояния агента."""

from typing import TypedDict, Literal, Optional

from src.agent.schemas import IntentClassification, AgentResponse, ErrorResponse, ApprovalParse


class AgentState(TypedDict):
    # === Пользователь ===
    user_id: str
    user_role: str  # "editor", "approver"
    user_branch: str
    has_attachment: bool
    file_path: str | None
    messages: list[dict]

    # === Роутинг ===
    intent: Literal[
        "get_plan",
        "submit_corrections",
        "approve_plan",
        "ask_status",
        "unclear",
    ] | None
    permission_granted: bool

    # === Structured Output: классификация ===
    intent_data: IntentClassification | None  # полный объект от LLM

    # === План ===
    target_month: str | None  # "2025-07"
    plan_exists: bool | None
    plan_data: list[dict] | None

    # === Дедлайн ===
    deadline_ok: bool | None  # False если дедлайн прошёл
    deadline_info: dict | None  # {"deadline_display": "...", "days_left": N, "is_passed": bool}

    # === Валидация корректировок ===
    validation_passed: bool | None
    validation_errors: list[str] | None
    corrections_data: list[dict] | None  # распарсенные данные из файла
    corrections_file_content: list[dict] | None # распарсенные строки Excel

    # === Approve / Finalize flow ===
    approval_decision: Literal[
        "approve_branch",
        "reject_branch",
        "finalize_plan",
    ] | None
    target_branch_for_approval: str | None                    # для обратной совместимости
    target_branches_for_approval: list[str] | None            # множественный выбор
    rejection_reason: str | None
    approval_parse: ApprovalParse | None
    plan_finalized: bool
    plan_finalized_at: str | None

    # === Финальный ответ (Structured Output) ===
    response: AgentResponse | ErrorResponse | None

    # === Мета ===
    request_id: str
    is_error: bool  # флаг graceful degradation
    iteration: int