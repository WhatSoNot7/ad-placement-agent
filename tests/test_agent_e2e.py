"""End-to-end тесты агента."""

import pytest
from unittest.mock import MagicMock
from src.agent.graph import create_graph
from src.agent.state import AgentState

import src.tools.deadlines as tools_deadlines
import src.tools.plan_db as tools_plan_db

@pytest.fixture
def graph(monkeypatch):
    # Мокаем инструменты/LLM, чтобы тесты были детерминированы
    # 1) Классификатор интентов → get_plan / approve_plan / ask_status и т.п.
    from src.agent import structured_output as so

    # Мокаем parse_intent/классификатор внутри so._invoke_with_retry по operation
    original_invoke = so.StructuredOutputHandler._invoke_with_retry

    def invoke_mock(self, schema, messages, request_id, operation):
        if operation == "classify_intent":
            # Вернем минимальную модель с intent по фразе пользователя
            text = messages[-1].content if messages else ""
            class Dummy(schema):
                pass
            if "Покажи план" in text:
                return schema(intent="get_plan", target_month="2026-07", reasoning="test")
            if "Утверждаю план" in text or "утверждаю план" in text:
                return schema(intent="approve_plan", target_month="2026-07", reasoning="test")
            if "Покажи статус" in text or "статус" in text:
                return schema(intent="ask_status", target_month="2026-07", reasoning="test")
            return schema(intent="other", target_month=None, reasoning="test")
        if operation == "parse_approval_decision":
            # Вернем минимальную модель по фразе пользователя
            text = messages[-1].content if messages else ""
            class Dummy(schema):
                pass
            if "Утверди план" in text:
                # Вернем approve одного филиала по умолчанию
                return schema(decision="approve_branch", target_branches=["Новосибирск"], target_month="2026-07", reason=None, confidence=1.0)
            if "Отклони план" in text:
                # Вернем reject одного филиала по умолчанию
                return schema(decision="reject_branch", target_branches=["Новосибирск"], target_month="2026-07", reason=None, confidence=1.0)                
        return original_invoke(self, schema, messages, request_id, operation)

    monkeypatch.setattr(so.StructuredOutputHandler, "_invoke_with_retry", invoke_mock)

    # 2) Мокаем инструменты БД
    # get_finalize_status
    monkeypatch.setattr(
        tools_plan_db,
        "get_finalize_status",
        MagicMock(invoke=lambda payload: {"plan_finalized": True, "plan_finalized_at": "2026-07-26"}),
    )    
    # get_deadline_info
    monkeypatch.setattr(
        tools_deadlines,
        "get_deadline_info",
        MagicMock(invoke=lambda payload: {"deadline_display": "25.07", "days_left": 10, "is_passed": False}),
    )
    # query_plan_db
    monkeypatch.setattr(
        tools_plan_db,
        "query_plan_db",
        MagicMock(invoke=lambda payload: {"status": "exists", "data": [{"channel": "search", "budget": 1000}]}),
    )
    # get_corrections_status_from_log
    monkeypatch.setattr(
        tools_plan_db,
        "get_corrections_status_from_log",
        MagicMock(invoke=lambda payload: {
            "total_branches": 2,
            "branches": {
                "Новосибирск": {"submitted": True, "status": "pending"},
                "Казань": {"submitted": False, "status": "pending"},
            }
        }),
    )
    # approve_corrections_for_branch
    monkeypatch.setattr(
        tools_plan_db,
        "approve_corrections_for_branch",
        MagicMock(invoke=lambda payload: {"success": True, **payload.get("params", {})}),
    )
    # reject_corrections_for_branch
    monkeypatch.setattr(
        tools_plan_db,
        "reject_corrections_for_branch",
        MagicMock(invoke=lambda payload: {"success": True, **payload.get("params", {})}),
    )
    # finalize_month_plan
    monkeypatch.setattr(
        tools_plan_db,
        "finalize_month_plan",
        MagicMock(invoke=lambda payload: {"success": True, "target_month": payload.get("params", {}).get("target_month")}),
    )

    return create_graph()


@pytest.fixture
def base_state() -> AgentState:
    return {
        "request_id": "test-req-0001",
        "messages": [],
        "user_id": "",
        "user_role": None, # "editor" | "approver"
        "user_branch": None, # например, "Новосибирск"
        "has_attachment": False,
        "file_path": None,
        "messages": [],
        
        "intent": None,
        "permission_granted": False,
        
        # контекст месяца/плана
        "target_month": None,         # "YYYY-MM"
        "plan_exists": None,
        "plan_data": None,

        # статус дедлайна/финализации
        "deadline_ok": None,          # если у вас остался этот флаг; иначе уберите
        "ready_to_finalize": False,

        # загрузка/валидация корректировок
        "corrections_data": None,
        "corrections_file_content": None,
        "validation_result": None,

        # согласование
        "all_corrections_received": False,

        # служебное
        "iteration": 0,
        "is_error": False,
        # если граф/узлы используют:
        "response": None,
        "approval_decision": None,
        "target_branch_for_approval": None,
        "target_branches_for_approval": [],
        "rejection_reason": None,
    }

def test_editor_get_existing_plan(graph, base_state):
    """Editor запрашивает существующий план — получает данные."""
    state = {
        **base_state,
        "messages": [{"role": "user", "content": "Покажи план на июль по Новосибирску"}],
        "user_id": "editor_nsk_01",
        "user_role": "editor",
        "user_branch": "Новосибирск",
    }
    result = graph.invoke(state)
    assert result["intent"] == "get_plan"
    assert result["permission_granted"] is True
    assert result["plan_exists"] is True
    assert result["plan_data"] is not None
    assert len(result["plan_data"]) > 0
    
def test_editor_cannot_approve(graph, base_state):
    """Editor пытается утвердить план — отказ по правам."""
    state = {
        **base_state,
        "messages": [{"role": "user", "content": "Утверждаю план на июль"}],
        "user_id": "editor_nsk_01",
        "user_role": "editor",
        "user_branch": "Новосибирск",
    }
    result = graph.invoke(state)
    assert result["intent"] == "approve_plan"
    assert result["permission_granted"] is False
    
def test_approver_can_approve(graph, base_state):
    """Approver может работать с утверждением (минимальный позитивный путь)."""
    state = {
        **base_state,
        "messages": [{"role": "user", "content": "Покажи статус корректировок на июль"}],
        "user_id": "approver_01",
        "user_role": "approver",
        "user_branch": "Новосибирск",
    }
    result = graph.invoke(state)
    assert result["permission_granted"] is True
    # Наличие ответа
    assert "response" in result and result["response"] is not None
    
def test_status_reports_finalized(graph, base_state, monkeypatch):
    """Если месяц финализирован — агент явно сообщает об этом и не предлагает финализацию."""
    # Переключаем инструмент статуса финализации на true
    tools_plan_db.get_finalize_status.invoke = MagicMock(
        side_effect=lambda payload: {"plan_finalized": True, "plan_finalized_at": "2026-07-26 10:10:10"}
    )
    state = {
        **base_state,
        "messages": [{"role": "user", "content": "Покажи статус корректировок на июль"}],
        "user_id": "editor_nsk_01",
        "user_role": "editor",
        "user_branch": "Новосибирск",
        "target_month": "2026-07",
    }
    result = graph.invoke(state)

    resp = result.get("response")
    assert resp is not None
    text = resp.message or ""
    # Сообщение должно явно содержать информацию о финализации
    assert "Итоговая версия зафиксирована" in text or "финализ" in text.lower()
    # В next_steps не должно быть предложения финализировать
    steps = resp.next_steps or []
    assert not any("финализировать" in s.lower() for s in steps)
    
def test_reject_requires_reason(graph, base_state, monkeypatch):
    """При отклонении без причины агент запрашивает уточнение."""
    # Подменим парсер решения, чтобы он вернул reject_branch без reason
    from src.agent import structured_output as so
    def invoke_mock(self, schema, messages, request_id, operation):
        if operation == "classify_intent":
            return schema(intent="approve_plan", target_month="2026-07", reasoning="test")
        if operation == "parse_approval_decision":
            return schema(decision="reject_branch", target_branches=["Новосибирск"], reason=None, confidence=0.9)
        return so.StructuredOutputHandler._invoke_with_retry(self, schema, messages, request_id, operation)

    monkeypatch.setattr(so.StructuredOutputHandler, "_invoke_with_retry", invoke_mock)

    state = {
        **base_state,
        "messages": [{"role": "user", "content": "Отклонить Новосибирск"}],
        "user_id": "approver_01",
        "user_role": "approver",
        "user_branch": "Новосибирск",
        "target_month": "2026-07",
    }
    result = graph.invoke(state)

    resp = result.get("response")
    assert resp is not None
    assert resp.status.name in ("NEEDS_CLARIFICATION", "WARNING")
    assert "причин" in resp.message.lower() or "укажите причину" in resp.message.lower()