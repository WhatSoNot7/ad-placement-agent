"""LangGraph граф агента."""

from langgraph.graph import StateGraph, END

from src.agent.state import AgentState
from src.agent.nodes import (
    identify_user,
    classify_intent,
    check_permissions,
    handle_get_plan,
    handle_submit_corrections,
    handle_ask_status,
    handle_approve_plan,
    handle_unclear,
    generate_response,
)


def route_by_permission(state: AgentState) -> str:
    """Роутинг после проверки прав."""
    if state.get("permission_granted"):
        return "route_intent"
    return END


def route_by_intent(state: AgentState) -> str:
    """Роутинг по намерению пользователя."""
    intent = state.get("intent", "unclear")
    routes = {
        "get_plan": "handle_get_plan",
        "submit_corrections": "handle_submit_corrections",
        "approve_plan": "handle_approve_plan",
        "ask_status": "handle_ask_status",
        "unclear": "handle_unclear",
    }
    return routes.get(intent, "handle_unclear")


def create_graph():
    """Создаёт и компилирует граф агента."""
    graph = StateGraph(AgentState)

    # Добавляем узлы
    graph.add_node("identify_user", identify_user)
    graph.add_node("classify_intent", classify_intent)
    graph.add_node("check_permissions", check_permissions)
    graph.add_node("handle_get_plan", handle_get_plan)
    graph.add_node("handle_submit_corrections", handle_submit_corrections)
    graph.add_node("handle_ask_status", handle_ask_status)
    graph.add_node("handle_approve_plan", handle_approve_plan)
    graph.add_node("handle_unclear", handle_unclear)

    # Определяем рёбра
    graph.set_entry_point("identify_user")
    graph.add_edge("identify_user", "classify_intent")
    graph.add_edge("classify_intent", "check_permissions")

    # Условное ребро: проверка прав
    graph.add_conditional_edges(
        "check_permissions",
        route_by_permission,
        {
            "route_intent": "route_intent_node",
            END: END,
        },
    )

    # Виртуальный узел для роутинга по intent
    # (используем conditional_edges напрямую из check_permissions)
    # Переделываем: убираем виртуальный узел, роутим прямо

    # Пересобираем без виртуального узла:
    graph = StateGraph(AgentState)

    graph.add_node("identify_user", identify_user)
    graph.add_node("classify_intent", classify_intent)
    graph.add_node("check_permissions", check_permissions)
    graph.add_node("handle_get_plan", handle_get_plan)
    graph.add_node("handle_submit_corrections", handle_submit_corrections)
    graph.add_node("handle_ask_status", handle_ask_status)
    graph.add_node("handle_approve_plan", handle_approve_plan)
    graph.add_node("handle_unclear", handle_unclear)

    # Линейная цепочка
    graph.set_entry_point("identify_user")
    graph.add_edge("identify_user", "classify_intent")
    graph.add_edge("classify_intent", "check_permissions")

    # ВЕТВЛЕНИЕ 1: права
    graph.add_conditional_edges(
        "check_permissions",
        route_by_permission,
        {
            "route_intent": "handle_get_plan",  # placeholder, переопределяется ниже
            END: END,
        },
    )

    # Нам нужно двухступенчатое ветвление.
    # LangGraph не поддерживает два conditional_edges из одного узла.
    # Решение: объединяем проверку прав и роутинг в одну функцию.

    # === ФИНАЛЬНАЯ ПРАВИЛЬНАЯ ВЕРСИЯ ===
    graph = StateGraph(AgentState)

    graph.add_node("identify_user", identify_user)
    graph.add_node("classify_intent", classify_intent)
    graph.add_node("check_permissions", check_permissions)
    graph.add_node("handle_get_plan", handle_get_plan)
    graph.add_node("handle_submit_corrections", handle_submit_corrections)
    graph.add_node("handle_ask_status", handle_ask_status)
    graph.add_node("handle_approve_plan", handle_approve_plan)
    graph.add_node("handle_unclear", handle_unclear)
    # генерация ответа
    graph.add_node("generate_response", generate_response_node)

    graph.set_entry_point("identify_user")
    graph.add_edge("identify_user", "classify_intent")
    graph.add_edge("classify_intent", "check_permissions")

    # Комбинированный роутинг: права + intent
    def combined_router(state: AgentState) -> str:
        if not state.get("permission_granted"):
            return END
        intent = state.get("intent", "unclear")
        routes = {
            "get_plan": "handle_get_plan",
            "submit_corrections": "handle_submit_corrections",
            "approve_plan": "handle_approve_plan",
            "ask_status": "handle_ask_status",
            "unclear": "handle_unclear",
        }
        return routes.get(intent, "handle_unclear")

    graph.add_conditional_edges(
        "check_permissions",
        combined_router,
        {
            "handle_get_plan": "handle_get_plan",
            "handle_submit_corrections": "handle_submit_corrections",
            "handle_approve_plan": "handle_approve_plan",
            "handle_ask_status": "handle_ask_status",
            "handle_unclear": "handle_unclear",
            END: END,
        },
    )

   
    # Все обработчики ведут к generate_response
    graph.add_edge("handle_get_plan", "generate_response")
    graph.add_edge("handle_submit_corrections", "generate_response")
    graph.add_edge("handle_ask_status", "generate_response")
    graph.add_edge("handle_approve_plan", "generate_response")
    graph.add_edge("handle_unclear", "generate_response")
    
    # generate_response -> END
    graph.add_edge("generate_response", END)

    return graph.compile()