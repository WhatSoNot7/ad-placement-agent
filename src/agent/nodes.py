"""Логика узлов графа."""

import json
import uuid
import logging
import os
from datetime import datetime

from langchain_core.messages import HumanMessage, AIMessage

from src.agent.state import AgentState
from src.agent.schemas import (
    IntentClassification,
    AgentResponse,
    ErrorResponse,
    ActionStatus,
    UserIntent,
)
from src.agent.structured_output import StructuredOutputHandler
from src.agent.prompts import SYSTEM_PROMPT, CLASSIFY_INTENT_PROMPT, RESPONSE_PROMPT
from src.tools.plan_db import query_plan_db
from src.tools.excel_export import export_plan_to_excel
from src.tools.validate_corrections import validate_corrections_file
from src.tools.deadlines import get_deadline_info
from src.tools.notifications import send_notification
from src.models.mock_forecast import recalculate_with_corrections
from src.config import get_llm

logger = logging.getLogger(__name__)


def _get_handler() -> StructuredOutputHandler:
    """Фабрика для создания StructuredOutputHandler с настройками из env."""
    return StructuredOutputHandler(
        model_name=os.environ.get("LLM_MODEL", "gpt-4o-mini"),
        temperature=float(os.environ.get("LLM_TEMPERATURE", "0.0")),
        max_retries=int(os.environ.get("LLM_MAX_RETRIES", "2")),
        developer_email=os.environ.get("DEVELOPER_EMAIL"),
    )


# ============================================================
# NODE: identify_user
# ============================================================

def identify_user(state: AgentState) -> dict:
    """Идентификация пользователя."""
    request_id = state.get("request_id") or str(uuid.uuid4())
    logger.info(
        f"[{request_id}] User identified: "
        f"role={state['user_role']}, branch={state['user_branch']}"
    )
    return {
        "request_id": request_id,
        "is_error": False,
        "iteration": state.get("iteration", 0),
    }


# ============================================================
# NODE: classify_intent (STRUCTURED OUTPUT + RETRY)
# ============================================================

def classify_intent(state: AgentState) -> dict:
    """
    Классификация намерения через StructuredOutputHandler.

    Используется with_structured_output(strict=True) — OpenAI гарантирует
    соответствие Pydantic-схеме IntentClassification.
    При провале всех retry — graceful degradation.
    """
    handler = _get_handler()
    request_id = state["request_id"]

    # Получаем последнее сообщение пользователя
    last_message = ""
    for msg in reversed(state.get("messages", [])):
        if msg.get("role") == "user":
            last_message = msg.get("content", "")
            break

    result = await handler.classify_intent(
        user_message=last_message,
        user_role=state["user_role"],
        user_branch=state["user_branch"],
        has_attachment=state.get("has_attachment", False),
        request_id=request_id,
    )

    # Если graceful degradation — формируем ErrorResponse и ставим флаг
    if isinstance(result, ErrorResponse):
        logger.error(f"[{request_id}] classify_intent failed -> graceful degradation")
        return {
            "intent": "unclear",
            "intent_data": None,
            "target_month": None,
            "response": result,
            "is_error": True,
        }

    # Успешная классификация
    logger.info(
        f"[{request_id}] Intent: {result.intent.value}, "
        f"month={result.target_month}, reasoning={result.reasoning}"
    )
    return {
        "intent": result.intent.value,
        "intent_data": result,
        "target_month": result.target_month,
        "is_error": False,
    }


# ============================================================
# NODE: check_permissions (ВЕТВЛЕНИЕ 1: denied / allowed)
# ============================================================

def check_permissions(state: AgentState) -> dict:
    """Проверка прав доступа."""
    if state.get("is_error"):
        return {"permission_granted": False}

    intent = state.get("intent", "unclear")
    user_role = state["user_role"]

    # Только manager/director могут утверждать планы
    if intent == "approve_plan" and user_role not in ("manager", "director"):
        logger.warning(
            f"[{state['request_id']}] Permission denied: "
            f"{user_role} cannot approve"
        )
        response = AgentResponse(
            message="У вас недостаточно прав для утверждения планов. "
                    "Эта функция доступна только руководителям.",
            status=ActionStatus.ERROR,
            action_performed="check_permissions",
            data_summary=None,
            next_steps=["Обратитесь к вашему руководителю"],
            requires_user_action=False,
        )
        return {"permission_granted": False, "response": response}

    return {"permission_granted": True}


# ============================================================
# NODE: handle_get_plan
# Ветвление: exists / not_ready / not_found
# ============================================================

def handle_get_plan(state: AgentState) -> dict:
    """
    Получение плана размещения.

    Ветвление по результату:
    - exists -> Export Excel + Send
    - not_ready (слишком рано) -> Respond: План ещё не сформирован
    - not_found (должен быть) -> Предложить уведомить автора модели
    """
    request_id = state["request_id"]
    branch = state["user_branch"]
    target_month = state.get("target_month")

    logger.info(f"[{request_id}] Getting plan: branch={branch}, month={target_month}")

    try:
        plan_result = query_plan_db.invoke({
            "branch": branch,
            "month": target_month,
        })

        # plan_result может вернуть: {"status": "exists"/"not_ready"/"not_found", "data": [...]}
        status = plan_result.get("status", "not_found") if isinstance(plan_result, dict) else "not_found"
        plan_data = plan_result.get("data", []) if isinstance(plan_result, dict) else plan_result

        if status == "exists" and plan_data:
            # План существует -> экспорт в Excel
            file_path = export_plan_to_excel.invoke({
                "plan_data": plan_data,
                "branch": branch,
                "month": target_month,
            })
            return {
                "plan_exists": True,
                "plan_data": plan_data,
                "tool_result": (
                    f"План найден. Записей: {len(plan_data)}. "
                    f"Файл сформирован: {file_path}"
                ),
            }

        elif status == "not_ready":
            # План ещё не сформирован (слишком рано)
            return {
                "plan_exists": False,
                "plan_data": None,
                "tool_result": (
                    f"План на {target_month or 'запрошенный месяц'} ещё не сформирован. "
                    f"Обычно планы появляются после 20-го числа предыдущего месяца. "
                    f"Ожидайте."
                ),
            }

        else:
            # not_found — план должен быть, но его нет
            # Предлагаем уведомить автора модели
            send_notification.invoke({
                "recipient_role": "model_author",
                "branch": branch,
                "message": (
                    f"Филиал '{branch}' запросил план на {target_month}, "
                    f"но план не найден в базе."
                ),
                "notification_type": "info",
            })
            return {
                "plan_exists": False,
                "plan_data": None,
                "tool_result": (
                    f"План для филиала '{branch}' на {target_month or 'текущий месяц'} "
                    f"не найден в базе данных. "
                    f"Автор модели уведомлён о проблеме."
                ),
            }

    except Exception as e:
        logger.error(f"[{request_id}] Error in handle_get_plan: {e}")
        return {
            "plan_exists": None,
            "plan_data": None,
            "tool_result": f"Ошибка при получении плана: {str(e)}",
        }


# ============================================================
# NODE: handle_submit_corrections
# ВЕТВЛЕНИЕ 2: Дедлайн (passed / ok)
# Затем: errors / valid
# ============================================================

def handle_submit_corrections(state: AgentState) -> dict:
    """
    Приём и валидация корректировок.

    Последовательность:
    1. Проверка дедлайна (ВЕТВЛЕНИЕ 2: passed / ok)
    2. Validate Excel
    3. Ветвление: errors / valid
    4. При valid: Run forecast -> Generate Comparison -> Save -> Notify Approver
    """
    request_id = state["request_id"]
    file_path = state.get("file_path")
    branch = state["user_branch"]
    target_month = state.get("target_month") or _default_month()

    # --- Нет файла ---
    if not file_path:
        response = AgentResponse(
            message="Файл корректировок не приложен. Пожалуйста, прикрепите файл Excel.",
            status=ActionStatus.NEEDS_CLARIFICATION,
            action_performed="submit_corrections",
            data_summary=None,
            next_steps=["Прикрепите файл Excel с корректировками и отправьте повторно"],
            requires_user_action=True,
        )
        return {
            "deadline_ok": None,
            "validation_result": None,
            "corrections_file_content": None,
            "response": response,
        }

    logger.info(f"[{request_id}] Processing corrections: file={file_path}, branch={branch}")

    # --- ВЕТВЛЕНИЕ 2: Проверка дедлайна ---
    try:
        deadline_info = get_deadline_info.invoke({
            "branch": branch,
            "month": target_month,
        })
        deadline_ok = not deadline_info.get("is_passed", False)
    except Exception as e:
        logger.warning(f"[{request_id}] Error checking deadline: {e}")
        deadline_info = None
        deadline_ok = True  # При ошибке пропускаем проверку

    if not deadline_ok:
        # Дедлайн прошёл (passed)
        response = AgentResponse(
            message=(
                f"Дедлайн для подачи корректировок истёк "
                f"({deadline_info.get('deadline_display', 'N/A')}). "
                f"Обратитесь к руководителю для продления срока."
            ),
            status=ActionStatus.ERROR,
            action_performed="check_deadline",
            data_summary=f"Дедлайн: {deadline_info.get('deadline_display', 'N/A')}",
            next_steps=["Обратитесь к руководителю для продления срока"],
            requires_user_action=True,
        )
        return {
            "deadline_ok": False,
            "validation_result": None,
            "corrections_file_content": None,
            "response": response,
        }

    # --- Дедлайн ok -> Validate Excel ---
    try:
        file_content = _read_file(file_path)
        validation = validate_corrections_file.invoke({
            "file_content": file_content,
            "branch": branch,
            "month": target_month,
        })
    except Exception as e:
        logger.error(f"[{request_id}] Error validating file: {e}")
        response = AgentResponse(
            message=f"Ошибка при обработке файла: {str(e)}",
            status=ActionStatus.ERROR,
            action_performed="validate_file",
            data_summary=None,
            next_steps=["Проверьте формат файла и попробуйте снова"],
            requires_user_action=True,
        )
        return {
            "deadline_ok": True,
            "validation_result": None,
            "corrections_file_content": None,
            "response": response,
        }

    # --- Ветвление: errors / valid ---
    if not validation.get("valid", False):
        errors = validation.get("errors", [])
        errors_text = "\n".join(
            f"  - Строка {e['row']}, '{e['column']}': {e['message']}"
            for e in errors[:10]
        )
        total_errors = len(errors)
        suffix = (
            f"\n  ... и ещё {total_errors - 10} ошибок"
            if total_errors > 10
            else ""
        )
        response = AgentResponse(
            message=(
                f"Найдены ошибки в файле:\n"
                f"{errors_text}{suffix}\n\n"
                f"Исправьте и пришлите повторно."
            ),
            status=ActionStatus.ERROR,
            action_performed="validate_file",
            data_summary=f"Ошибок: {total_errors}, строк обработано: {validation.get('total_rows', 0)}",
            next_steps=["Исправьте указанные ошибки", "Пришлите файл повторно"],
            requires_user_action=True,
        )
        return {
            "deadline_ok": True,
            "validation_result": validation,
            "corrections_file_content": None,
            "response": response,
        }

    # --- Valid: Run forecast -> Save -> Notify Approver ---
    corrections_data = validation.get("data", [])

    try:
        forecast_result = recalculate_with_corrections.invoke({
            "branch": branch,
            "corrections": corrections_data,
        })
        comparison = forecast_result.get("comparison_report", "")
        budget_delta = forecast_result.get("budget_delta", 0.0)
        leads_delta = forecast_result.get("leads_delta", 0.0)
    except Exception as e:
        logger.error(f"[{request_id}] Error recalculating forecast: {e}")
        comparison = f"Ошибка пересчёта: {str(e)}"
        budget_delta = None
        leads_delta = None

    # Notify Approver
    try:
        send_notification.invoke({
            "recipient_id": "approver",
            "message": (
                f"Новые корректировки от филиала '{branch}' "
                f"на {target_month} ожидают утверждения. "
                f"Дельта бюджета: {budget_delta}, дельта лидов: {leads_delta}."
            ),
            "notification_type": "action_required",
        })
    except Exception as e:
        logger.warning(f"[{request_id}] Failed to notify approver: {e}")

    response = AgentResponse(
        message=(
            f"Корректировки приняты и прошли валидацию.\n"
            f"Строк: {len(corrections_data)}.\n"
            f"Прогноз пересчитан. {comparison}\n"
            f"Руководитель уведомлён и рассмотрит корректировки."
        ),
        status=ActionStatus.SUCCESS,
        action_performed="submit_corrections",
        data_summary=(
            f"Строк: {len(corrections_data)}, "
            f"дельта бюджета: {budget_delta}, дельта лидов: {leads_delta}"
        ),
        next_steps=["Ожидайте решения руководителя"],
        requires_user_action=False,
    )
    return {
        "deadline_ok": True,
        "validation_result": validation,
        "corrections_file_content": corrections_data,
        "response": response,
    }


# ============================================================
# NODE: handle_ask_status
# ============================================================

def handle_ask_status(state: AgentState) -> dict:
    """
    Информирование о статусе процесса и дедлайнах.
    """
    request_id = state["request_id"]
    branch = state["user_branch"]
    target_month = state.get("target_month") or _default_month()

    logger.info(f"[{request_id}] Getting status: branch={branch}, month={target_month}")

    try:
        deadline_info = get_deadline_info.invoke({
            "branch": branch,
            "month": target_month,
        })

        # Получаем статус плана
        plan_result = query_plan_db.invoke({
            "branch": branch,
            "month": target_month,
        })
        plan_status = plan_result.get("status", "unknown") if isinstance(plan_result, dict) else "unknown"

        # Формируем статусную информацию
        deadline_display = deadline_info.get("deadline_display", "N/A")
        days_left = deadline_info.get("days_left", "N/A")
        is_passed = deadline_info.get("is_passed", False)
        approval_status = state.get("approval_status", "pending")

        status_text = (
            f"Статус на {target_month}:\n"
            f"  - План: {plan_status}\n"
            f"  - Дедлайн корректировок: {deadline_display}\n"
            f"  - {'⚠️ Дедлайн прошёл' if is_passed else f'Осталось дней: {days_left}'}\n"
            f"  - Статус утверждения: {approval_status}"
        )

        response = AgentResponse(
            message=status_text,
            status=ActionStatus.SUCCESS,
            action_performed="get_status",
            data_summary=f"Месяц: {target_month}, план: {plan_status}, утверждение: {approval_status}",
            next_steps=[],
            requires_user_action=False,
        )

    except Exception as e:
        logger.error(f"[{request_id}] Error in handle_ask_status: {e}")
        response = AgentResponse(
            message=f"Не удалось получить информацию о статусе: {str(e)}",
            status=ActionStatus.ERROR,
            action_performed="get_status",
            data_summary=None,
            next_steps=["Попробуйте позже"],
            requires_user_action=False,
        )

    return {"response": response}


# ============================================================
# NODE: handle_approve_plan
# ВЕТВЛЕНИЕ 3: Все корректировки получены / дедлайн
# ВЕТВЛЕНИЕ 4: Решение руководителя
# ============================================================

def handle_approve_plan(state: AgentState) -> dict:
    """
    Утверждение/отклонение корректировок руководителем.

    ВЕТВЛЕНИЕ 3: Проверяем, все ли филиалы прислали корректировки / дедлайн вышел
      - Не все ответили -> Показать статус, предложить подождать/напомнить
      - Все получены / дедлайн вышел -> Перейти к решению

    ВЕТВЛЕНИЕ 4: Решение руководителя
      - approve_branch -> Утвердить филиал + Notify Editor: Принято
      - reject_branch -> Отклонить филиал + Notify Editor: Отклонено + причина
      - request_modify -> Запросить доработку + Notify Editor: Доработать
      - approve_all -> Сохранить финальный план в БД + Notify All: План утверждён
      - reject_all -> Откат к базовому плану
    """
    request_id = state["request_id"]
    branch = state["user_branch"]
    user_role = state["user_role"]
    target_month = state.get("target_month") or _default_month()
    last_message = state["messages"][-1]["content"].lower()

    logger.info(f"[{request_id}] Approve flow: role={user_role}, branch={branch}")

    # --- Получаем статус корректировок по всем филиалам ---
    try:
        corrections_status = query_plan_db.invoke({
            "action": "get_corrections_status",
            "month": target_month,
        })
        branch_statuses = corrections_status.get("branches", {})
        total_branches = corrections_status.get("total_branches", 0)
        submitted_count = sum(
            1 for s in branch_statuses.values()
            if s.get("submitted", False)
        )
        all_corrections_received = submitted_count >= total_branches
    except Exception as e:
        logger.error(f"[{request_id}] Error getting corrections status: {e}")
        branch_statuses = {}
        total_branches = 0
        submitted_count = 0
        all_corrections_received = False

    # --- Проверка дедлайна для финализации ---
    try:
        deadline_info = get_deadline_info.invoke({
            "branch": branch,
            "month": target_month,
        })
        deadline_passed = deadline_info.get("is_passed", False)
    except Exception as e:
        logger.warning(f"[{request_id}] Error checking deadline for approve: {e}")
        deadline_passed = False

    # === ВЕТВЛЕНИЕ 3: Все корректировки получены / дедлайн ===
    if not all_corrections_received and not deadline_passed:
        # Не все editor-ы ответили, дедлайн не вышел
        pending_branches = [
            b for b, s in branch_statuses.items()
            if not s.get("submitted", False)
        ]
        response = AgentResponse(
            message=(
                f"Не все филиалы прислали корректировки.\n"
                f"Получено: {submitted_count}/{total_branches}.\n"
                f"Ожидаем от: {', '.join(pending_branches)}.\n"
                f"Дедлайн: {deadline_info.get('deadline_display', 'N/A')}.\n\n"
                f"Можете подождать или напомнить филиалам."
            ),
            status=ActionStatus.PARTIAL,
            action_performed="check_corrections_status",
            data_summary=f"Получено: {submitted_count}/{total_branches}",
            next_steps=[
                "Подождать поступления корректировок",
                "Напомнить филиалам о дедлайне",
            ],
            requires_user_action=True,
        )
        return {
            "all_corrections_received": False,
            "approval_decision": None,
            "response": response,
        }

    # === ВЕТВЛЕНИЕ 4: Определение решения руководителя ===
    decision = _parse_approval_decision(last_message, state)

    if decision is None:
        # Решение ещё не принято — показываем статус и запрашиваем действие
        statuses_text = "\n".join(
            f"  - {b}: {'получено' if s.get('submitted') else 'не получено'}"
            for b, s in branch_statuses.items()
        )
        reason = "Все корректировки получены." if all_corrections_received else "Дедлайн вышел."
        response = AgentResponse(
            message=(
                f"{reason}\n"
                f"Статус по филиалам:\n{statuses_text}\n\n"
                f"Выберите действие:\n"
                f"  1. Утвердить конкретный филиал\n"
                f"  2. Отклонить конкретный филиал (с указанием причины)\n"
                f"  3. Запросить доработку у филиала\n"
                f"  4. Утвердить все\n"
                f"  5. Отклонить все (откат к базовому плану)"
            ),
            status=ActionStatus.NEEDS_CLARIFICATION,
            action_performed="show_corrections_status",
            data_summary=f"Филиалов: {total_branches}, получено: {submitted_count}",
            next_steps=[
                "Утвердить филиал: 'утвердить [название филиала]'",
                "Отклонить: 'отклонить [филиал], причина: ...'",
                "Доработка: 'доработать [филиал]'",
                "Утвердить все: 'утвердить все'",
                "Отклонить все: 'отклонить все'",
            ],
            requires_user_action=True,
        )
        return {
            "all_corrections_received": all_corrections_received,
            "approval_decision": None,
            "response": response,
        }

    # === Выполнение решения ===
    return await _execute_approval_decision(
        decision=decision,
        state=state,
        request_id=request_id,
        branch_statuses=branch_statuses,
        target_month=target_month,
    )


def _execute_approval_decision(
    decision: str,
    state: AgentState,
    request_id: str,
    branch_statuses: dict,
    target_month: str,
) -> dict:
    """Выполнить решение руководителя."""

    target_branch = state.get("target_branch_for_approval", state["user_branch"])
    rejection_reason = state.get("rejection_reason", "")

    if decision == "approve_branch":
        # Утвердить конкретный филиал
        try:
            query_plan_db.invoke({
                "action": "approve_branch",
                "branch": target_branch,
                "month": target_month,
            })
            send_notification.invoke({
                "recipient_role": "editor",
                "branch": target_branch,
                "message": f"Ваши корректировки на {target_month} утверждены.",
                "notification_type": "success",
            })
        except Exception as e:
            logger.error(f"[{request_id}] Error approving branch: {e}")

        response = AgentResponse(
            message=f"Корректировки филиала '{target_branch}' утверждены. Редактор уведомлён.",
            status=ActionStatus.SUCCESS,
            action_performed="approve_branch",
            data_summary=f"Филиал: {target_branch}, месяц: {target_month}",
            next_steps=["Продолжить рассмотрение других филиалов"],
            requires_user_action=False,
        )

    elif decision == "reject_branch":
        # Отклонить конкретный филиал
        try:
            query_plan_db.invoke({
                "action": "reject_branch",
                "branch": target_branch,
                "month": target_month,
                "reason": rejection_reason,
            })
            send_notification.invoke({
                "recipient_role": "editor",
                "branch": target_branch,
                "message": (
                    f"Ваши корректировки на {target_month} отклонены.\n"
                    f"Причина: {rejection_reason or 'не указана'}"
                ),
                "notification_type": "rejection",
            })
        except Exception as e:
            logger.error(f"[{request_id}] Error rejecting branch: {e}")

        response = AgentResponse(
            message=(
                f"Корректировки филиала '{target_branch}' отклонены.\n"
                f"Причина: {rejection_reason or 'не указана'}.\n"
                f"Редактор уведомлён."
            ),
            status=ActionStatus.SUCCESS,
            action_performed="reject_branch",
            data_summary=f"Филиал: {target_branch}, причина: {rejection_reason}",
            next_steps=["Продолжить рассмотрение других филиалов"],
            requires_user_action=False,
        )

    elif decision == "request_modify":
        # Запросить доработку
        try:
            send_notification.invoke({
                "recipient_role": "editor",
                "branch": target_branch,
                "message": (
                    f"Корректировки на {target_month} требуют доработки.\n"
                    f"Комментарий: {rejection_reason or 'Свяжитесь с руководителем для уточнения.'}"
                ),
                "notification_type": "action_required",
            })
        except Exception as e:
            logger.error(f"[{request_id}] Error requesting modification: {e}")

        response = AgentResponse(
            message=(
                f"Запрос на доработку отправлен в филиал '{target_branch}'.\n"
                f"Комментарий: {rejection_reason or 'не указан'}"
            ),
            status=ActionStatus.SUCCESS,
            action_performed="request_modify",
            data_summary=f"Филиал: {target_branch}",
            next_steps=["Ожидать обновлённые корректировки от филиала"],
            requires_user_action=False,
        )

    elif decision == "approve_all":
        # Утвердить все — сохранить финальный план в БД
        try:
            query_plan_db.invoke({
                "action": "finalize_plan",
                "month": target_month,
            })
            send_notification.invoke({
                "recipient_role": "all",
                "branch": "all",
                "message": f"Финальный план на {target_month} утверждён.",
                "notification_type": "success",
            })
        except Exception as e:
            logger.error(f"[{request_id}] Error finalizing plan: {e}")

        response = AgentResponse(
            message=(
                f"Все корректировки утверждены.\n"
                f"Финальный план на {target_month} сохранён в базе данных.\n"
                f"Все участники уведомлены."
            ),
            status=ActionStatus.SUCCESS,
            action_performed="approve_all",
            data_summary=f"Месяц: {target_month}, утверждено филиалов: {len(branch_statuses)}",
            next_steps=[],
            requires_user_action=False,
        )

    elif decision == "reject_all":
        # Откат к базовому плану
        try:
            query_plan_db.invoke({
                "action": "rollback_to_base",
                "month": target_month,
            })
            send_notification.invoke({
                "recipient_role": "all",
                "branch": "all",
                "message": (
                    f"Все корректировки на {target_month} отклонены. "
                    f"Выполнен откат к базовому плану."
                ),
                "notification_type": "rejection",
            })
        except Exception as e:
            logger.error(f"[{request_id}] Error rolling back: {e}")

        response = AgentResponse(
            message=(
                f"Все корректировки отклонены.\n"
                f"Выполнен откат к базовому плану на {target_month}.\n"
                f"Все участники уведомлены."
            ),
            status=ActionStatus.SUCCESS,
            action_performed="reject_all",
            data_summary=f"Месяц: {target_month}, откат к базовому плану",
            next_steps=[],
            requires_user_action=False,
        )

    else:
        response = AgentResponse(
            message="Не удалось определить ваше решение. Пожалуйста, уточните действие.",
            status=ActionStatus.NEEDS_CLARIFICATION,
            action_performed=None,
            data_summary=None,
            next_steps=[
                "Утвердить филиал: 'утвердить [название]'",
                "Отклонить: 'отклонить [филиал], причина: ...'",
                "Утвердить все / Отклонить все",
            ],
            requires_user_action=True,
        )

    return {
        "all_corrections_received": True,
        "approval_decision": decision,
        "rejection_reason": state.get("rejection_reason"),
        "response": response,
    }


# ============================================================
# NODE: handle_unclear
# ============================================================

def handle_unclear(state: AgentState) -> dict:
    """Обработка неясного запроса или запроса не по теме."""
    response = AgentResponse(
        message=(
            "Я не совсем понял ваш запрос. Я могу помочь с:\n"
            "  - Получение плана размещения (скажите 'покажи план на июль')\n"
            "  - Отправка корректировок (прикрепите файл Excel)\n"
            "  - Статус процесса (спросите 'какой статус?' или 'когда дедлайн?')\n"
            "  - Утверждение планов (для руководителей)\n\n"
            "Пожалуйста, уточните ваш запрос."
        ),
        status=ActionStatus.NEEDS_CLARIFICATION,
        action_performed=None,
        data_summary=None,
        next_steps=[
            "Уточните запрос",
            "Используйте одну из команд выше",
        ],
        requires_user_action=True,
    )
    return {"response": response}


# ============================================================
# NODE: generate_response (STRUCTURED OUTPUT + RETRY)
# Используется когда tool-ноды записали tool_result в state,
# но не сформировали готовый response
# ============================================================

def generate_response(state: AgentState) -> dict:
    """
    Генерация финального ответа через StructuredOutputHandler.

    Вызывается только если предыдущие ноды НЕ сформировали response напрямую.
    Берёт tool_result из state и формирует AgentResponse через LLM.
    При провале всех retry — graceful degradation.
    """
    # Если response уже сформирован (напрямую в tool-ноде или graceful degradation)
    if state.get("response") is not None:
        return {}

    # Если произошла ошибка на этапе classify_intent
    if state.get("is_error"):
        return {}

    handler = _get_handler()
    request_id = state["request_id"]

    intent = state.get("intent", "unknown")
    tool_result = state.get("tool_result", "Нет данных")

    result = await handler.generate_response(
        user_role=state["user_role"],
        user_branch=state["user_branch"],
        action=intent,
        result=tool_result,
        request_id=request_id,
    )

    return {"response": result}


# ============================================================
# HELPER: _parse_approval_decision
# ============================================================

def _parse_approval_decision(last_message: str, state: AgentState) -> str | None:
    """
    Определить решение руководителя из текста сообщения.

    Returns:
        approve_branch | reject_branch | request_modify | approve_all | reject_all | None
    """
    msg = last_message.lower().strip()

    # approve_all / reject_all — проверяем первыми (более специфичные)
    if any(phrase in msg for phrase in ["утвердить все", "принять все", "утвердить всё", "принять всё"]):
        return "approve_all"

    if any(phrase in msg for phrase in ["отклонить все", "отклонить всё", "откатить", "откат"]):
        return "reject_all"

    # request_modify
    if any(phrase in msg for phrase in ["доработать", "исправить", "переделать", "на доработку"]):
        return "request_modify"

    # reject_branch
    if any(phrase in msg for phrase in ["отклонить", "отказать", "не принимаю", "отказ"]):
        return "reject_branch"

    # approve_branch
    if any(phrase in msg for phrase in ["утвердить", "принять", "ок", "согласен", "одобрить"]):
        return "approve_branch"

    return None


# ============================================================
# HELPER: _default_month
# ============================================================

def _default_month() -> str:
    """Вернуть текущий месяц в формате YYYY-MM."""
    now = datetime.now()
    return now.strftime("%Y-%m")


# ============================================================
# HELPER: _read_file
# ============================================================

def _read_file(file_path: str) -> bytes | None:
    """Прочитать файл и вернуть содержимое."""
    try:
        with open(file_path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        logger.error(f"File not found: {file_path}")
        return None
    except IOError as e:
        logger.error(f"Error reading file {file_path}: {e}")
        return None