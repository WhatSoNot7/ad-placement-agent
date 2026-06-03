"""Логика узлов графа."""

import json
import logging
from datetime import datetime

from langchain_core.messages import HumanMessage, AIMessage

from src.agent.state import AgentState
from src.agent.prompts import SYSTEM_PROMPT, CLASSIFY_INTENT_PROMPT, RESPONSE_PROMPT
from src.tools.plan_db import query_plan_db
from src.tools.excel_export import export_plan_to_excel
from src.tools.validate_corrections import validate_corrections_file
from src.tools.deadlines import get_deadline_info
from src.tools.notifications import send_notification
from src.models.mock_forecast import recalculate_with_corrections
from src.config import get_llm

logger = logging.getLogger(__name__)

# Справочник пользователей (в production — из БД)
USERS_DB = {
    "editor_nsk_01": {"name": "Иванов И.И.", "branch": "Новосибирск", "role": "editor"},
    "editor_kzn_01": {"name": "Петров П.П.", "branch": "Казань", "role": "editor"},
    "editor_msk_01": {"name": "Сидоров С.С.", "branch": "Москва", "role": "editor"},
    "approver_01": {"name": "Директоров Д.Д.", "branch": "HQ", "role": "approver"},
}

ROLE_PERMISSIONS = {
    "editor": {"get_plan", "submit_corrections", "ask_status"},
    "approver": {"get_plan", "submit_corrections", "ask_status", "approve_plan"},
}


def identify_user(state: AgentState) -> dict:
    """Определяет роль и филиал пользователя."""
    user_id = state["user_id"]
    user_info = USERS_DB.get(user_id)

    if user_info is None:
        return {
            "user_role": None,
            "user_branch": None,
            "messages": state["messages"] + [
                {"role": "assistant", "content": "Не удалось определить вашу учётную запись. Обратитесь к администратору."}
            ],
        }

    return {
        "user_role": user_info["role"],
        "user_branch": user_info["branch"],
    }


def classify_intent(state: AgentState) -> dict:
    """LLM классифицирует намерение пользователя."""
    llm = get_llm()

    last_message = state["messages"][-1]["content"] if state["messages"] else ""
    has_attachment = state.get("corrections_file_content") is not None

    prompt = CLASSIFY_INTENT_PROMPT.format(
        user_role=state.get("user_role", "unknown"),
        user_branch=state.get("user_branch", "unknown"),
        has_attachment=has_attachment,
        user_message=last_message,
    )

    response = llm.invoke([
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ])

    # Парсим JSON из ответа
    try:
        content = response.content.strip()
        # Убираем markdown code block если есть
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("```", 1)[0]
        result = json.loads(content)
        intent = result.get("intent", "unclear")
        target_month = result.get("target_month")
    except (json.JSONDecodeError, AttributeError):
        intent = "unclear"
        target_month = None

    return {
        "intent": intent,
        "target_month": target_month,
    }


def check_permissions(state: AgentState) -> dict:
    """Проверяет, имеет ли пользователь право на запрошенное действие."""
    role = state.get("user_role")
    intent = state.get("intent")

    if role is None:
        return {"permission_granted": False}

    allowed = ROLE_PERMISSIONS.get(role, set())
    granted = intent in allowed

    if not granted:
        return {
            "permission_granted": False,
            "messages": state["messages"] + [
                {"role": "assistant", "content": f"У вас нет прав для выполнения этого действия. Ваша роль: {role}. Обратитесь к руководителю."}
            ],
        }

    return {"permission_granted": True}


def handle_get_plan(state: AgentState) -> dict:
    """Обрабатывает запрос на получение плана."""
    branch = state["user_branch"]
    month = state.get("target_month")

    if not month:
        # Если месяц не указан — берём текущий + 1
        now = datetime.now()
        if now.month == 12:
            month = f"{now.year + 1}-01"
        else:
            month = f"{now.year}-{now.month + 1:02d}"

    # Вызываем tool
    result = query_plan_db.invoke({"branch": branch, "month": month})

    status = result["status"]
    plan_data = result.get("data", [])

    if status == "exists":
        # Экспортируем в Excel
        export_result = export_plan_to_excel.invoke({
            "plan_data": plan_data,
            "branch": branch,
            "month": month,
        })

        response_text = (
            f"План на {month} по филиалу {branch} готов.\n"
            f"Домов в плане: {len(plan_data)}\n"
            f"Файл: {export_result.get('file_path', 'не удалось сформировать')}\n\n"
            f"Дедлайн для корректировок — уточните командой 'статус'."
        )

        return {
            "plan_exists": True,
            "plan_data": plan_data,
            "messages": state["messages"] + [
                {"role": "assistant", "content": response_text}
            ],
        }

    elif status == "not_ready":
        return {
            "plan_exists": False,
            "plan_data": None,
            "messages": state["messages"] + [
                {"role": "assistant", "content": result["message"]}
            ],
        }

    else:  # not_found
        # Предлагаем уведомить автора
        send_notification.invoke({
            "recipient_id": "model_author",
            "message": f"План на {month} по филиалу {branch} отсутствует. Запрос от {state['user_id']}.",
            "notification_type": "action_required",
        })

        return {
            "plan_exists": False,
            "plan_data": None,
            "messages": state["messages"] + [
                {"role": "assistant", "content": result["message"] + " Автор модели уведомлён."}
            ],
        }


def handle_submit_corrections(state: AgentState) -> dict:
    """Обрабатывает отправку корректировок."""
    branch = state["user_branch"]
    month = state.get("target_month") or _default_month()

    # 1. Проверяем дедлайн
    deadline_info = get_deadline_info.invoke({"month": month, "branch": branch})

    if deadline_info["is_passed"]:
        return {
            "deadline_ok": False,
            "messages": state["messages"] + [
                {"role": "assistant", "content": (
                    f"Дедлайн корректировок на {month} истёк "
                    f"({deadline_info['deadline_display']}). "
                    f"Корректировки больше не принимаются."
                )}
            ],
        }

    # 2. Проверяем наличие файла
    file_content = state.get("corrections_file_content")
    if not file_content:
        return {
            "deadline_ok": True,
            "messages": state["messages"] + [
                {"role": "assistant", "content": (
                    f"Дедлайн: {deadline_info['deadline_display']} "
                    f"(осталось {deadline_info['days_left']} дн.). "
                    f"Приложите файл .xlsx с корректировками."
                )}
            ],
        }

    # 3. Валидируем
    validation = validate_corrections_file.invoke({
        "file_content": file_content,
        "branch": branch,
        "month": month,
    })

    if not validation["valid"]:
        errors_text = "\n".join(
            f"  - Строка {e['row']}, '{e['column']}': {e['message']}"
            for e in validation["errors"][:10]
        )
        total_errors = len(validation["errors"])
        suffix = (
            f"\n  ... и ещё {total_errors - 10} ошибок"
            if total_errors > 10
            else ""
        )
        return {
            "deadline_ok": True,
            "validation_result": validation,
            "messages": state["messages"] + [
                {"role": "assistant", "content": (
                    f"Найдены ошибки в файле:\n"
                    f"{errors_text}{suffix}\n\n"
                    f"Исправьте и пришлите повторно."
                )}
            ],
        }

    # 4. Пересчитываем прогноз
    original_plan_result = query_plan_db.invoke({
        "branch": branch,
        "month": month,
    })
    original_plan = original_plan_result.get("data", [])
    corrections = validation["corrections_parsed"]

    adjusted_plan, summary = recalculate_with_corrections(
        original_plan, corrections
    )

    # 5. Формируем отчёт сравнения
    comparison_text = (
        f"✅ Корректировки приняты и пересчитаны.\n\n"
        f"**Изменения:**\n"
        f"- Исключено домов: {summary['removed_count']}\n"
        f"- Добавлено домов: {summary['added_count']}\n"
        f"- Изменено домов: {summary['modified_count']}\n\n"
        f"**Прогноз заявок:**\n"
        f"- Было: {summary['original_leads_total']}\n"
        f"- Стало: {summary['adjusted_leads_total']} "
        f"({summary['leads_delta']:+.1f})\n\n"
        f"**Бюджет:**\n"
        f"- Было: {summary['original_cost_total']:.0f} ₽\n"
        f"- Стало: {summary['adjusted_cost_total']:.0f} ₽ "
        f"({summary['cost_delta']:+.0f} ₽)\n\n"
        f"Корректировки отправлены на утверждение."
    )

    # 6. Уведомляем approver'а
    send_notification.invoke({
        "recipient_id": "approver_01",
        "message": (
            f"Получены корректировки от {state['user_id']} "
            f"по филиалу {branch} на {month}. "
            f"Изменение заявок: {summary['leads_delta']:+.1f}, "
            f"бюджета: {summary['cost_delta']:+.0f} ₽."
        ),
        "notification_type": "action_required",
    })

    return {
        "deadline_ok": True,
        "validation_result": validation,
        "adjusted_plan": adjusted_plan,
        "comparison_report": comparison_text,
        "budget_delta": summary["cost_delta"],
        "leads_delta": summary["leads_delta"],
        "messages": state["messages"] + [
            {"role": "assistant", "content": comparison_text}
        ],
    }

# ============================================================
# NODE: handle_approve_plan
# ============================================================
def handle_approve_plan(state: AgentState) -> dict:
    """Обрабатывает утверждение/отклонение корректировок."""
    last_message = state["messages"][-1]["content"].lower()

    # Простая логика определения решения
    if any(word in last_message for word in ["утвердить", "принять", "ок", "согласен"]):
        decision = "approved"
    elif any(word in last_message for word in ["отклонить", "отказ", "нет", "не принимаю"]):
        decision = "rejected"
    elif any(word in last_message for word in ["доработать", "исправить", "изменить"]):
        decision = "modify"
    else:
        return {
            "messages": state["messages"] + [
                {"role": "assistant", "content": (
                    "Уточните решение:\n"
                    "- «Утвердить» — принять корректировки\n"
                    "- «Отклонить» — отказать\n"
                    "- «Доработать» — вернуть на доработку"
                )}
            ],
        }

    if decision == "approved":
        response = "✅ Корректировки утверждены. План обновлён."
        send_notification.invoke({
            "recipient_id": state["user_id"],
            "message": "Ваши корректировки утверждены.",
            "notification_type": "info",
        })
    elif decision == "rejected":
        response = "❌ Корректировки отклонены. План остаётся без изменений."
        send_notification.invoke({
            "recipient_id": state["user_id"],
            "message": "Ваши корректировки отклонены.",
            "notification_type": "warning",
        })
    else:
        response = "🔄 Корректировки возвращены на доработку."
        send_notification.invoke({
            "recipient_id": state["user_id"],
            "message": "Корректировки возвращены на доработку.",
            "notification_type": "action_required",
        })

    return {
        "approval_status": decision,
        "messages": state["messages"] + [
            {"role": "assistant", "content": response}
        ],
    }


# ============================================================
# NODE: handle_ask_status
# ============================================================
def handle_ask_status(state: AgentState) -> dict:
    """Возвращает статус процесса."""
    branch = state["user_branch"]
    month = state.get("target_month") or _default_month()

    deadline_info = get_deadline_info.invoke({"month": month, "branch": branch})

    status_text = (
        f"📋 Статус процесса ({month}, {branch}):\n\n"
        f"**Дедлайн корректировок:** {deadline_info['deadline_display']}\n"
    )

    if deadline_info["is_passed"]:
        status_text += "⏰ Дедлайн прошёл. Корректировки не принимаются.\n"
    else:
        status_text += f"⏳ Осталось {deadline_info['days_left']} дн.\n"

    approval = state.get("approval_status")
    if approval:
        status_text += f"\n**Статус утверждения:** {approval}\n"
    else:
        status_text += "\n**Статус утверждения:** ожидает\n"

    return {
        "messages": state["messages"] + [
            {"role": "assistant", "content": status_text}
        ],
    }


# ============================================================
# NODE: handle_unclear
# ============================================================
def handle_unclear(state: AgentState) -> dict:
    """Обрабатывает неясный запрос."""
    return {
        "messages": state["messages"] + [
            {"role": "assistant", "content": (
                "Не совсем понял ваш запрос. Я могу помочь с:\n\n"
                "📋 **Получить план** — напишите 'покажи план на [месяц]'\n"
                "📝 **Отправить корректировки** — приложите файл .xlsx\n"
                "📊 **Статус процесса** — напишите 'статус'\n"
                "✅ **Утвердить план** — напишите 'утвердить' (только для руководителей)\n\n"
                "Уточните, что вам нужно?"
            )}
        ],
    }


# ============================================================
# NODE: handle_ask_status
# ============================================================
def handle_ask_status(state: AgentState) -> dict:
    """Возвращает статус процесса."""
    branch = state["user_branch"]
    month = state.get("target_month") or _default_month()
    role = state["user_role"]

    # Получаем дедлайн
    deadline_info = get_deadline_info.invoke({"month": month, "branch": branch})

    # Формируем статус
    status_parts = [
        f"📊 **Статус плана на {month}**\n",
        f"Филиал: {branch}",
        f"Дедлайн корректировок: {deadline_info['deadline_display']}",
    ]

    if deadline_info["is_passed"]:
        status_parts.append("⏰ Дедлайн прошёл. Корректировки не принимаются.")
    else:
        status_parts.append(f"⏳ Осталось дней: {deadline_info['days_left']}")

    # Если approver — показываем статус по всем филиалам
    if role == "approver":
        branch_statuses = state.get("branch_statuses", {})
        if branch_statuses:
            status_parts.append("\n**Статус по филиалам:**")
            for br, st in branch_statuses.items():
                submitted = "✅" if st.get("submitted") else "⏳"
                approved = ""
                if st.get("approved") is True:
                    approved = " → утверждено ✅"
                elif st.get("approved") is False:
                    approved = " → отклонено ❌"
                status_parts.append(f"  {submitted} {br}{approved}")
        else:
            status_parts.append("\nКорректировки пока не поступали.")

        if state.get("ready_to_finalize"):
            status_parts.append("\n✅ Можно финализировать план.")

    return {
        "messages": state["messages"] + [
            {"role": "assistant", "content": "\n".join(status_parts)}
        ],
    }


# ============================================================
# NODE: handle_approve_plan
# ============================================================
def handle_approve_plan(state: AgentState) -> dict:
    """Обрабатывает запрос на утверждение плана (только approver)."""
    branch_statuses = state.get("branch_statuses", {})
    ready = state.get("ready_to_finalize", False)

    # Если нет корректировок для утверждения
    if not branch_statuses:
        return {
            "messages": state["messages"] + [
                {"role": "assistant", "content": (
                    "Пока нет корректировок для утверждения. "
                    "Ожидаем отправку от ответственных сотрудников."
                )}
            ],
        }

    # Показываем сводку
    summary_parts = ["📋 **Корректировки для утверждения:**\n"]

    for br, st in branch_statuses.items():
        if st.get("submitted") and st.get("approved") is None:
            summary_parts.append(
                f"  • {br}: ожидает решения\n"
                f"    Изменений: {st.get('changes_count', '?')}\n"
                f"    Прогноз заявок: {st.get('leads_delta', '?'):+.1f}"
            )

    if ready:
        summary_parts.append(
            "\n\n✅ Все корректировки получены. "
            "Можете финализировать план командой 'финализировать'."
        )
    else:
        pending = [br for br, st in branch_statuses.items() if not st.get("submitted")]
        if pending:
            summary_parts.append(f"\n\n⏳ Ожидаем корректировки от: {', '.join(pending)}")

    return {
        "approval_status": "pending",
        "messages": state["messages"] + [
            {"role": "assistant", "content": "\n".join(summary_parts)}
        ],
    }


# ============================================================
# HELPER: _default_month
# ============================================================
def _default_month() -> str:
    """Возвращает следующий месяц в формате YYYY-MM."""
    now = datetime.now()
    if now.month == 12:
        return f"{now.year + 1}-01"
    return f"{now.year}-{now.month + 1:02d}"