"""Streamlit UI для чат-бота."""

import streamlit as st
import asyncio
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.agent.graph import create_graph
from src.agent.state import AgentState


st.set_page_config(
    page_title="Ad Placement Agent",
    page_icon="📊",
    layout="wide",
)

st.title("📊 Агент планирования рекламы")
st.caption("Чат-бот для работы с планами размещения рекламы домашнего интернета")


# --- Sidebar: выбор пользователя ---
st.sidebar.header("Настройки")

USER_OPTIONS = {
    "editor_nsk_01": "Иванов И.И. (Editor, Новосибирск)",
    "editor_kzn_01": "Петрова А.С. (Editor, Казань)",
    "editor_msk_01": "Сидоров К.В. (Editor, Москва)",
    "approver_01": "Козлова М.Н. (Approver, HQ)",
}

selected_user = st.sidebar.selectbox(
    "Пользователь:",
    options=list(USER_OPTIONS.keys()),
    format_func=lambda x: USER_OPTIONS[x],
)

st.sidebar.divider()
st.sidebar.markdown(
    f"**Роль:** `{'approver' if 'approver' in selected_user else 'editor'}`"
)


# --- Chat history ---
if "messages" not in st.session_state:
    st.session_state.messages = []

if "graph" not in st.session_state:
    st.session_state.graph = create_graph()

# Display chat history
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])


# --- File upload ---
uploaded_file = st.sidebar.file_uploader(
    "Загрузить файл корректировок (.xlsx)",
    type=["xlsx"],
    key="corrections_file",
)


# --- Chat input ---
if prompt := st.chat_input("Введите сообщение..."):
    # Show user message
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    # Prepare state
    initial_state: AgentState = {
        "messages": [{"role": "user", "content": prompt}],
        "user_id": selected_user,
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

    # Handle file attachment
    if uploaded_file is not None:
        import openpyxl
        import io

        wb = openpyxl.load_workbook(io.BytesIO(uploaded_file.read()))
        ws = wb.active
        headers = [cell.value for cell in ws[1]]
        rows = []
        for row in ws.iter_rows(min_row=2, values_only=True):
            rows.append(dict(zip(headers, row)))
        initial_state["corrections_file_content"] = rows

    # Run agent
    with st.chat_message("assistant"):
        with st.spinner("Думаю..."):
            try:
                result = st.session_state.graph.invoke(initial_state)

                # Extract response
                if result.get("messages"):
                    last_msg = result["messages"][-1]
                    if hasattr(last_msg, "content"):
                        response = last_msg.content
                    else:
                        response = str(last_msg.get("content", ""))
                else:
                    response = "Не удалось получить ответ."

                st.markdown(response)
                st.session_state.messages.append(
                    {"role": "assistant", "content": response}
                )

                # Show file download if available
                if result.get("plan_data") and result.get("plan_exists"):
                    st.download_button(
                        label="📥 Скачать план (Excel)",
                        data=open(
                            f"data/exports/plan_{result.get('user_branch')}_{result.get('target_month')}.xlsx",
                            "rb",
                        ).read(),
                        file_name=f"plan_{result.get('target_month')}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )

            except Exception as e:
                error_msg = f"⚠️ Ошибка: {str(e)}"
                st.error(error_msg)
                st.session_state.messages.append(
                    {"role": "assistant", "content": error_msg}
                )