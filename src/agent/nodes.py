"""Логика узлов графа."""

import json
import uuid
import logging
import os
from datetime import datetime
from datetime import date

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
from src.tools.plan_db import (
    get_finalize_status,
    query_plan_db, 
    save_corrections_to_db, 
    get_corrections_status_from_log, 
    approve_corrections_for_branch, 
    reject_corrections_for_branch, 
    finalize_month_plan,
)
from src.tools.excel_export import export_plan_to_excel
from src.tools.validate_corrections import validate_corrections_file
from src.tools.deadlines import get_deadline_info
from src.tools.notifications import send_notification
from src.models.mock_forecast import recalculate_with_corrections

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
        #"is_error": False,
        #"iteration": state.get("iteration", 0),
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

    result = handler.classify_intent(
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
    if intent == "approve_plan" and user_role not in ("approver"):
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
    Возвращает в state:
    - plan_exists: bool | None
    - plan_data: list | None
    - file_path: str | None
    - response: AgentResponse
    """
    request_id = state["request_id"]
    branch = state["user_branch"]
    target_month = state.get("target_month")

    logger.info(f"[{request_id}] Getting plan: branch={branch}, month={target_month}")

    # 1) Нет месяца — просим уточнить
    if not target_month:
        response = AgentResponse(
            message=(
                "Не удалось определить целевой месяц. "
                "Уточните, за какой месяц нужен план (например: 'план на 2026‑07')."
            ),
            status=ActionStatus.NEEDS_CLARIFICATION,
            action_performed="clarify_month_for_plan",
            data_summary=None,
            next_steps=["Уточните месяц в формате ГГГГ‑ММ"],
            requires_user_action=True,
        )
        return {"plan_exists": None, "plan_data": None, "file_path": None, "response": response}

    try:
        # 2) Получаем план из БД
        raw = query_plan_db.invoke({"branch": branch, "month": target_month})
        plan_result = raw if isinstance(raw, dict) else {}
        status = plan_result.get("status", "not_found")
        plan_data = plan_result.get("data") if isinstance(plan_result.get("data"), list) else []

        # 3) Ветки по статусу
        if status == "exists" and plan_data:
            # Экспорт в Excel
            fp_raw = export_plan_to_excel.invoke({"plan_data": plan_data, "branch": branch, "month": target_month})
            file_path = fp_raw.get("file_path") if isinstance(fp_raw, dict) else fp_raw

            msg = f"План для филиала {branch} за {target_month} найден. Записей: {len(plan_data)}."
            if file_path:
                msg += " Файл готов к скачиванию."

            response = AgentResponse(
                message=msg,
                status=ActionStatus.SUCCESS,
                action_performed="get_plan",
                data_summary=f"Месяц: {target_month}, филиал: {branch}, план: exists",
                next_steps=[],
                requires_user_action=False,
            )
            return {"plan_exists": True, "plan_data": plan_data, "file_path": file_path, "response": response}

        if status == "not_ready":
            msg = (
                f"План на {target_month} ещё не сформирован. "
                "Обычно планы появляются после 20‑го числа предыдущего месяца."
            )
            response = AgentResponse(
                message=msg,
                status=ActionStatus.INFO if hasattr(ActionStatus, "INFO") else ActionStatus.SUCCESS,
                action_performed="get_plan",
                data_summary=f"Месяц: {target_month}, филиал: {branch}, план: not_ready",
                next_steps=["Проверить позже"],
                requires_user_action=False,
            )
            return {"plan_exists": False, "plan_data": None, "file_path": None, "response": response}

        # not_found или любой другой статус
        user_id = state.get("user_id") or "unknown"
        try:
            send_notification.invoke({
                "recipient_role": "model_author",
                "recipient_id": user_id,
                "branch": branch,
                "message": f"Запрошен план {branch} на {target_month}, но запись не найдена.",
                "notification_type": "info",
            })
        except Exception as ne:
            logger.warning(f"[{request_id}] notify model_author failed: {ne}")

        msg = (
            f"План для филиала {branch} на {target_month} не найден в базе данных. "
            "Автор модели уведомлён."
        )
        response = AgentResponse(
            message=msg,
            status=ActionStatus.WARNING if hasattr(ActionStatus, "WARNING") else ActionStatus.SUCCESS,
            action_performed="get_plan",
            data_summary=f"Месяц: {target_month}, филиал: {branch}, план: not_found",
            next_steps=["Повторить запрос позже", "Связаться с автором модели"],
            requires_user_action=False,
        )
        return {"plan_exists": False, "plan_data": None, "file_path": None, "response": response}

    except Exception as e:
        logger.error(f"[{request_id}] Error in handle_get_plan: {e}")
        response = AgentResponse(
            message=f"Не удалось получить план: {e}",
            status=ActionStatus.ERROR,
            action_performed="get_plan",
            data_summary=f"Месяц: {target_month}, филиал: {branch}",
            next_steps=["Попробовать позже"],
            requires_user_action=False,
        )
        return {"plan_exists": None, "plan_data": None, "file_path": None, "response": response}


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
    editor_id = state["user_id"]
    branch = state["user_branch"]
    target_month = state.get("target_month")

    has_attachment=state.get("has_attachment", False)
    file_path = state.get("file_path")
    file_content = state.get("corrections_file_content")
    
    # --- Проблемы с файлом ---
    if not has_attachment:
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
    elif not file_content:
        response = AgentResponse(
            message="Файл не удалось прочитать. Проверьте формат (xlsx, первая строка — заголовки).",
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
        if not isinstance(file_content, list):
            raise ValueError("Ожидался распарсенный Excel в виде list[dict] в state['corrections_file_content'].")
        validation = validate_corrections_file.with_config(run_name="validate_corrections").invoke({
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
    corrections_parsed = validation.get("corrections_parsed", [])

    try:
        adjusted_plan, forecast_summary = recalculate_with_corrections(
        branch=branch,
        month=target_month,
        corrections=corrections_parsed,
        )
        comparison = forecast_summary.get("comparison_report", "")
        budget_delta = forecast_summary.get("cost_delta")
        leads_delta = forecast_summary.get("leads_delta")
    except Exception as e:
        logger.error(f"[{request_id}] Error recalculating forecast: {e}")
        comparison = f"Ошибка пересчёта: {str(e)}"
        budget_delta = None
        leads_delta = None
        
    # Сохраняем корректировки в БД: corrections_log
    try:
        save_corrections_to_db_result = save_corrections_to_db.invoke({
            "branch": branch,
            "month": target_month,
            "editor_id": editor_id,
            "corrections": corrections_parsed, # или adjusted_plan, если нужно сохранять итоговый план
        })
        saved_to_db = save_corrections_to_db_result["success"]
    except Exception as e:
        logger.error(f"[{request_id}] Failed to save corrections: {e}")
    
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
            "Корректировки приняты и прошли валидацию.\n"
            f"Строк: {len(corrections_parsed)}.\n"
            "Прогноз пересчитан.\n\n"
            f"{comparison}\n\n"
            f"Прогноз сохранен в БД: {saved_to_db}\n\n"
            "Руководитель уведомлён и рассмотрит корректировки."
        ),        
        status=ActionStatus.SUCCESS,
        action_performed="submit_corrections",
        data_summary=(
            f"Строк: {len(corrections_parsed)}, "
            f"дельта бюджета: {budget_delta}, дельта лидов: {leads_delta}"
        ),
        next_steps=["Ожидайте решения руководителя"],
        requires_user_action=False,
    )
    return {
        "deadline_ok": True,
        "validation_result": validation,
        "corrections_file_content": corrections_parsed,
        "response": response,
    }


# ============================================================
# NODE: handle_ask_status
# ============================================================

def handle_ask_status(state: AgentState) -> dict:
    """
    Информирование о статусе процесса и дедлайнах по текущему филиалу и общей сводке по корректировкам.
    """
    request_id = state["request_id"]
    role = state["user_role"] # "editor" | "approver" | др.
    branch = state["user_branch"]
    target_month = state.get("target_month")

    logger.info(f"[{request_id}] Getting status: role={role}, branch={branch}, month={target_month}")    

    try:
        # Проверяем финализирован ли план
        try:
            fin = get_finalize_status.invoke({"month": target_month})
            plan_finalized = bool(fin.get("plan_finalized")) if isinstance(fin, dict) else False
            plan_finalized_at = fin.get("plan_finalized_at") if isinstance(fin, dict) else None
        except Exception as e:
            logger.warning(f"[{request_id}] get_finalize_status failed: {e}")
            plan_finalized, plan_finalized_at = False, None
            
        # Дедлайн по текущему филиалу
        try:
            deadline_info = get_deadline_info.invoke({
                "branch": branch,
                "month": target_month,
            })
        except Exception as e:
            logger.warning(f"[{request_id}] get_deadline_info failed: {e}")
            deadline_info = {}

        deadline_display = deadline_info.get("deadline_display", "N/A")
        days_left = deadline_info.get("days_left", "N/A")
        is_passed = deadline_info.get("is_passed", False)

        # Статус плана по текущему филиалу
        try:
            plan_result = query_plan_db.invoke({
                "branch": branch,
                "month": target_month,
            })
            plan_status = plan_result.get("status", "unknown") if isinstance(plan_result, dict) else "unknown"
        except Exception as e:
            logger.warning(f"[{request_id}] query_plan_db (plan_status) failed: {e}")
            plan_status = "unknown"

        # Сводка по корректировкам из corrections_log (все филиалы за месяц)
        # Предполагаемый интерфейс tools: get_corrections_status_from_log.invoke({"month": target_month})
        try:
            corr = get_corrections_status_from_log.invoke({"month": target_month})
            branch_statuses = corr.get("branches", {}) if isinstance(corr, dict) else {}
            total_branches = corr.get("total_branches", len(branch_statuses))
            submitted_count = sum(1 for s in branch_statuses.values() if s.get("submitted"))
            no_changes = [b for b, s in branch_statuses.items() if not s.get("submitted")]
        except Exception as e:
            logger.warning(f"[{request_id}] get_corrections_status_from_log failed: {e}")
            branch_statuses, total_branches, submitted_count, no_changes = {}, 0, 0, []

        # Текст статуса
        status_lines = [
            f"Статус для {target_month or '—'}:",
            f" - План ({branch}): {plan_status}",
            f" - Дедлайн корректировок: {deadline_display}",
        ]
        
        if plan_finalized:
            when = f" от {plan_finalized_at}" if plan_finalized_at else ""
            status_lines.append(f" - Итоговая версия зафиксирована{when} — изменения недоступны")
        else:
            status_lines.append(f" - {'⚠️ Дедлайн прошёл' if is_passed else f'Осталось дней: {days_left}'}")

            if total_branches:
                status_lines.append(f" - Корректировки получены: {submitted_count}/{total_branches}")
                status_lines.append(f" - Без изменений: {', '.join(no_changes) if no_changes else '—'}")
        
        # Рекомендованные шаги — зависят от роли
        next_steps: list[str] = []
        if plan_finalized:
            # после финализации никаких действий по корректировкам
            if role == "approver":
                next_steps.append("Итоговая версия зафиксирована. Для правок создайте новый цикл корректировок.")
            else:
                next_steps.append("Итоговая версия зафиксирована. Новые корректировки недоступны.")
        else:
            if role == "approver":
                if is_passed:
                    next_steps.append("Финализировать план: напишите 'финализировать план'")
                    next_steps.extend([
                    "Утвердить филиал(ы): 'утвердить A; B'",
                    "Отклонить филиал(ы): 'отклонить A; B, причина: ...'",
                    ])
            else:
                if is_passed:
                    next_steps.append("Ожидайте решения руководителя по финализации")
                else:
                    next_steps.extend([
                        "При необходимости отправьте корректировки",
                        "Уточните дедлайн у руководителя при риске задержки",
                    ])

        summary_bits = [
            f"Месяц: {target_month or '—'}",
            f"план: {plan_status}",
            f"корректировки: {submitted_count}/{total_branches}",
            f"финализирован: {'да' if plan_finalized else 'нет'}",
        ]
        
        response = AgentResponse(
            message="\n".join(status_lines),
            status=ActionStatus.SUCCESS,
            action_performed="get_status",
            data_summary=", ".join(summary_bits),
            next_steps=next_steps,
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
    request_id = state["request_id"]
    user_branch = state["user_branch"]
    user_role = state["user_role"]
    target_month = state.get("target_month")
    reviewer_id = state.get("user_id") # approver ID 
    
    try:
        last_message = state["messages"][-1]["content"]
    except Exception as e:
        last_message = ""

    # 1) Статус по филиалам
    try:
        corrections_status = get_corrections_status_from_log.invoke({"month": target_month})
        branch_statuses = corrections_status.get("branches", {})  # {branch: {submitted: bool, ...}}
        total_branches = corrections_status.get("total_branches", len(branch_statuses))
        submitted_count = sum(1 for s in branch_statuses.values() if s.get("submitted"))
        no_changes = [b for b, s in branch_statuses.items() if not s.get("submitted")]
    except Exception as e:
        logger.error(f"[{request_id}] Error getting corrections status: {e}")
        branch_statuses, total_branches, submitted_count, no_changes = {}, 0, 0, []

    # 2) Дедлайн
    try:
        deadline_info = get_deadline_info.invoke({
            "branch": user_branch,
            "month": target_month,
        })
        deadline_passed = deadline_info.get("is_passed", False)
    except Exception as e:
        logger.warning(f"[{request_id}] Error checking deadline for approve: {e}")
        deadline_passed, deadline_info = False, {}

    available_branches = list(branch_statuses.keys()) or [user_branch]
    success_branches, failed_branches = [], []
    errors_by_branch = {}

    # 3) LLM-парсинг через StructuredOutputHandler
    handler = _get_handler()
    parsed = handler.parse_approval_decision(
        message=last_message,
        available_branches=available_branches,
        request_id=request_id,
    )
    print(f"[{request_id}] RETURN parse_approval_decision type={type(parsed)} val={parsed}", flush=True)
    
    # Если graceful degradation
    if isinstance(parsed, ErrorResponse) or parsed is None or not getattr(parsed, "decision", None):
        response = AgentResponse(
            message=(
                f"Статус корректировок на {target_month}:\n"
                f"Получено: {submitted_count}/{total_branches}.\n"
                f"Без изменений: {', '.join(no_changes) if no_changes else '—'}.\n\n"
                f"Доступные филиалы: {', '.join(available_branches)}.\n"
                f"Можно написать, например:\n"
                f"- 'утвердить Новосибирск; Казань'\n"
                f"- 'отклонить Москва; причина: перерасход бюджета'\n"
                f"- После дедлайна: 'финализировать план'"
            ),
            status=ActionStatus.NEEDS_CLARIFICATION,
            action_performed="show_corrections_status",
            data_summary=f"Получено: {submitted_count}/{total_branches}",
            next_steps=[
                "Утвердить филиалы: 'утвердить A; B; C'",
                "Отклонить филиалы: 'отклонить A; B; причина: ...'",
                "После дедлайна: 'финализировать план'",
            ],
            requires_user_action=True,
        )
        return {"approval_decision": None, "response": response}

    # Сохраняем парс в state
    state["approval_parse"] = parsed
    decision = parsed.decision
    target_branches = [b for b in (parsed.target_branches or []) if b in available_branches]
    rejection_reason = parsed.reason

    # 4) approve_branch / reject_branch
    if decision in ("approve_branch", "reject_branch"):
        if not target_branches:
            response = AgentResponse(
                message=f"Уточните филиал(ы). Доступные: {', '.join(available_branches)}",
                status=ActionStatus.NEEDS_CLARIFICATION,
                action_performed="clarify_branches",
                requires_user_action=True,
            )
            return {"approval_decision": None, "response": response}

        if decision == "reject_branch":
            
            def need_reason(reason: str | None) -> tuple[bool, str | None]:
                r = (reason or "").strip()
                if not r:
                    return True, None
                rl = r.lower()
                bad_fragments = (
                    "план отклонен",
                    "план по",          # «план по X отклонен»
                    "отклонен",         # общие формулировки без причины
                    "отклонить",
                    "отклоняю",
                )
                if any(x in rl for x in bad_fragments):
                    return True, r
                # допустим минимальную длину осмысленной причины
                if len(r) < 4:
                    return True, r
                return False, r
                
            need, norm_reason = need_reason(rejection_reason)
            if need:
                # попросим краткую предметную причину без служебных слов
                examples = [
                    "перерасход бюджета",
                    "низкие продажи",
                    "размещение недоступно",
                    "не согласовано юр. отделом",
                ]
                response = AgentResponse(
                    message=(
                        f"Нужна краткая причина отклонения для: {', '.join(target_branches)}.\n"
                        f"Например: {', '.join(examples)}.\n"
                        f"Формат: 'отклонить A; B; причина: <кратко>'."
                    ),
                    status=ActionStatus.NEEDS_CLARIFICATION,
                    action_performed="clarify_rejection_reason",
                    requires_user_action=True,
                )
                return {
                    "approval_decision": None,
                    "target_branches_for_approval": target_branches,
                    "response": response,
                }
            # зафиксируем нормализованную причину
            rejection_reason = norm_reason       
        
        # Применяем решения по каждому филиалу
        for b in target_branches:
            if decision == "approve_branch":
                try:
                    res = approve_corrections_for_branch.invoke({
                        "branch": b,
                        "month": target_month,
                        "reviewed_by": reviewer_id,
                    })
                except Exception as e:
                    logger.error(f"[{request_id}] Error approving {b}: {e}")
            else:
                try:
                    res = reject_corrections_for_branch.invoke({
                        "branch": b,
                        "month": target_month,
                        "reviewed_by": reviewer_id,
                        "reason": rejection_reason,
                    })
                    send_notification.invoke({
                        "recipient_id": "editor",
                        "message": (
                            f"Ваши корректировки на {target_month} отклонены.\n"
                            f"Причина: {rejection_reason}"
                        ),
                        "notification_type": "rejection",
                    })
                except Exception as e:
                    logger.error(f"[{request_id}] Error rejecting {b}: {e}")

            ok = bool(res.get("success", False))
            if ok:
                success_branches.append(b)
                # нотификации только при успехе
                send_notification.invoke({
                        "recipient_id": "editor",
                        "branch": b,
                        "message": (
                            f"Ваши корректировки на {target_month} "
                            f"{'утверждены' if decision == 'approve_branch' else 'отклонены'}."
                            + (f"\nПричина: {rejection_reason}" if decision == "reject_branch" else "")
                        ),
                        "notification_type": "success" if decision == "approve_branch" else "rejection",
                })
            else:
                failed_branches.append(b)
                errors_by_branch[b] = "не удалось применить действие"
                
        if success_branches and not failed_branches:
            status = ActionStatus.SUCCESS
            msg = (
                f"{'Утверждены' if decision == 'approve_branch' else 'Отклонены'}: {', '.join(success_branches)}."
                + (f"\nПричина: {rejection_reason}" if decision == "reject_branch" else "")
            )
        elif success_branches and failed_branches:
            status = ActionStatus.PARTIAL_SUCCESS if hasattr(ActionStatus, "PARTIAL_SUCCESS") else ActionStatus.WARNING
            failed_lines = [f"- {b}: {errors_by_branch.get(b, 'ошибка')}" for b in failed_branches]
            msg = "\n".join([
                f"Частично выполнено.",
                f"Успех: {', '.join(success_branches)}.",
                "Не удалось:",
                *failed_lines,
            ])
        else:
            status = ActionStatus.ERROR
            failed_lines = [f"- {b}: {errors_by_branch.get(b, 'ошибка')}" for b in failed_branches]
            msg = "\n".join([
                f"Не удалось выполнить действие: {decision}.",
                "\nПодробности:",
                *failed_lines,
            ])

        response = AgentResponse(
        message=msg.strip(),
        status=status,
        action_performed=decision,
        data_summary=f"Месяц: {target_month}, успех: {', '.join(success_branches) or '—'}, не удалось: {', '.join(failed_branches) or '—'}",
        next_steps=(["Финализировать план"] if deadline_passed and success_branches else ["После дедлайна сможете финализировать план"]),
        requires_user_action=(status != ActionStatus.SUCCESS),
        )

        return {
        "approval_decision": decision if success_branches else None,
        "target_branches_for_approval": success_branches,
        "rejection_reason": rejection_reason,
        "response": response,
        }

    # 5) finalize_plan
    if decision == "finalize_plan":
        if not deadline_passed:
            response = AgentResponse(
                message="Финализация доступна после истечения дедлайна. Пока можете утвердить/отклонить отдельные филиалы.",
                status=ActionStatus.NEEDS_CLARIFICATION,
                action_performed="too_early_to_finalize",
                requires_user_action=True,
            )
            return {"approval_decision": None, "response": response}

        try:
            finalize_month_plan.invoke({"month": target_month})
            send_notification.invoke({
                "recipient_id": "all",
                "message": f"Финальный план на {target_month} утверждён и сохранён.",
                "notification_type": "success",
            })
        except Exception as e:
            logger.error(f"[{request_id}] Error finalizing plan: {e}")
            response = AgentResponse(
                message=f"Ошибка финализации: {e}",
                status=ActionStatus.ERROR,
                action_performed="finalize_plan",
                requires_user_action=True,
            )
            return {"approval_decision": None, "response": response}

        response = AgentResponse(
            message=(
                f"Финальный план на {target_month} утверждён и сохранён в БД.\n"
                f"Все участники уведомлены."
            ),
            status=ActionStatus.SUCCESS,
            action_performed="finalize_plan",
            data_summary=f"Месяц: {target_month}",
            next_steps=[],
            requires_user_action=False,
        )
        return {"approval_decision": "finalize_plan", "response": response, "plan_finalized": True}

    # 6) Фолбэк
    response = AgentResponse(
        message="Уточните действие: 'утвердить A; B', 'отклонить A; B, причина: ...' или 'финализировать план'.",
        status=ActionStatus.NEEDS_CLARIFICATION,
        action_performed="unknown_decision",
        requires_user_action=True,
    )
    return {"approval_decision": None, "response": response}


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

    request_id = state["request_id"]

    # Для остальных случаев — LLM
    handler = _get_handler()
    tool_result = state.get("tool_result") or "Нет данных"

    result = handler.generate_response(
        user_role=state.get("user_role"),
        user_branch=state.get("user_branch"),
        target_month=state.get("target_month"),
        action=intent,
        result=tool_result,
        request_id=request_id,
    )
    return {"response": result}