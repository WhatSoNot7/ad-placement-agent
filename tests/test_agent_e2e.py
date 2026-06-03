"""End-to-end тесты агента."""

import pytest
from src.agent.graph import create_graph
from src.agent.state import AgentState


@pytest.fixture
def graph():
    return create_graph()


@pytest.fixture
def base_state() -> AgentState:
    return {
        "messages": [],
        "user_id": "",
        "user_role": None,
        "user_branch": None,
        "intent": None,
        "permission_granted": False,
        "target_month": None,
        "plan_exists": None,
        "plan_data": None,
        "deadline_ok": None,
        "corrections_file_content": None,
        "validation_result": None,
        "adjusted_plan": None,
        "comparison_report": None,
        "budget_delta": None,
        "leads_delta": None,
        "approval_status": None,
        "branch_statuses": {},
        "all_corrections_received": False,
        "ready_to_finalize": False,
        "iteration": 0,
    }


def test_editor_get_existing_plan(graph, base_state):
    """Editor запрашивает существующий план — получает данные."""
    state = {
        **base_state,
        "messages": [{"role": "user", "content": "Покажи план на июль по Новосибирску"}],
        "user_id": "editor_nsk_01",
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
    }
    result = graph.invoke(state)

    assert result["intent"] == "approve_plan"
    assert result["permission_granted"] is False


def test_approver_can_approve(graph, base_state):
    """Approver может работать с утверждением."""
    state = {
        **base_state,
        "messages": [{"role": "user", "content": "Покажи статус корректировок на июль"}],
        "user_id": "approver_01",
    }
    result = graph.invoke(state)

    assert result["permission_granted"] is True