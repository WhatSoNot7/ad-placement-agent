"""Определение состояния агента."""

from typing import TypedDict, Literal


class AgentState(TypedDict):
    # Сообщения чата
    messages: list[dict]

    # Пользователь
    user_id: str
    user_role: Literal["editor", "approver"] | None
    user_branch: str | None

    # Роутинг
    intent: Literal[
        "get_plan",
        "submit_corrections",
        "approve_plan",
        "ask_status",
        "unclear",
    ] | None
    permission_granted: bool

    # План
    target_month: str | None  # "2025-07"
    plan_exists: bool | None
    plan_data: list[dict] | None

    # Корректировки
    deadline_ok: bool | None
    corrections_file_content: list[dict] | None
    validation_result: dict | None

    # Пересчёт
    adjusted_plan: list[dict] | None
    comparison_report: str | None
    budget_delta: float | None
    leads_delta: float | None

    # Финализация
    approval_status: Literal["pending", "approved", "rejected", "modify"] | None
    branch_statuses: dict  # {"Новосибирск": {"submitted": True, "approved": None}, ...}
    all_corrections_received: bool
    ready_to_finalize: bool

    # Мета
    iteration: int