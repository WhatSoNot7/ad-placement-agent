"""Модуль structured output с retry и graceful degradation."""

import uuid
import logging
import traceback
from typing import Any, Optional, Type

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel

from src.agent.schemas import (
    IntentClassification,
    AgentResponse,
    ErrorResponse,
    ApprovalParse
)
from src.agent.notifications import send_error_notification_sync
from src.agent.prompts import CLASSIFY_INTENT_PROMPT, RESPONSE_PROMPT, SYSTEM_PROMPT, APPROVAL_DECISION_PROMPT
from src.config import get_llm, callbacks

from datetime import datetime, date, timezone
import json

logger = logging.getLogger(__name__)

# ============================================================
# HELPER: _default_month
# ============================================================
def _default_month() -> str:
    """Вернуть следующий месяц в формате YYYY-MM."""
    now = datetime.now()
    # Если текущий месяц декабрь (12), то следующий — январь (1) следующего года
    if now.month == 12:
        next_month_first_day = date(now.year + 1, 1, 1)
    else:
        next_month_first_day = date(now.year, now.month + 1, 1)
    
    return next_month_first_day.strftime("%Y-%m")
    
current_year = datetime.now().year

class StructuredOutputHandler:
    """Обработчик structured output с retry=2 и graceful degradation."""
    
    def __init__(
        self,
        model_name: str = "gpt-4o-mini",
        temperature: float = 0.0,
        max_retries: int = 2,
        developer_email: Optional[str] = None,
    ):
        self.llm = get_llm()
        self.max_retries = max_retries
        self.developer_email = developer_email
    
    def _get_structured_llm(self, schema: Type[BaseModel]):
        """Создать LLM с привязкой к конкретной Pydantic-схеме."""
        return self.llm.with_structured_output(
            schema,
            method="function_calling",  # вместо "json_schema"
            strict=True,
        )
    
    def classify_intent(
        self,
        user_message: str,
        user_role: str,
        user_branch: str,
        has_attachment: bool,
        request_id: Optional[str] = None,
    ) -> IntentClassification | ErrorResponse:
        """
        Классифицировать намерение пользователя.
        
        Returns:
            IntentClassification при успехе, ErrorResponse при неудаче
        """
        if request_id is None:
            request_id = str(uuid.uuid4())
        
        prompt = CLASSIFY_INTENT_PROMPT.format(
            user_role=user_role,
            user_branch=user_branch,
            has_attachment=has_attachment,
            user_message=user_message,
            current_year=current_year,
            default_month=_default_month(),
        )
        
        messages = [
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=prompt),
        ]
        
        return self._invoke_with_retry(
            schema=IntentClassification,
            messages=messages,
            request_id=request_id,
            operation="classify_intent",
        )
    
    def generate_response(
        self,
        user_role: str,
        user_branch: str,
        target_month: str,
        action: str,
        result: str,
        request_id: Optional[str] = None,
    ) -> AgentResponse | ErrorResponse:
        """
        Сгенерировать структурированный ответ пользователю.
        
        Returns:
            AgentResponse при успехе, ErrorResponse при неудаче
        """
        if request_id is None:
            request_id = str(uuid.uuid4())
        
        prompt = RESPONSE_PROMPT.format(
            user_role=user_role,
            user_branch=user_branch,
            target_month=target_month,
            action=action,
            result=result,
        )
        
        messages = [
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=prompt),
        ]
        
        return self._invoke_with_retry(
            schema=AgentResponse,
            messages=messages,
            request_id=request_id,
            operation="generate_response",
        )
    
    def _invoke_with_retry(
        self,
        schema: Type[BaseModel],
        messages: list,
        request_id: str,
        operation: str,
    ) -> BaseModel | ErrorResponse:
        """
        Вызвать LLM с retry-логикой.
        
        Попытки: 1 основная + max_retries повторных = 3 всего при max_retries=2
        """
        print(f"[{request_id}] _invoke_with_retry: START op={operation}", flush=True)
        # Локальная функция приведения любого ответа к нужной Pydantic‑модели
        def _coerce_to_schema(obj: Any) -> BaseModel:
            if isinstance(obj, schema):
                return obj
            if isinstance(obj, dict):
                return schema(**obj)

            to_dict = getattr(obj, "dict", None) or getattr(obj, "model_dump", None)
            if callable(to_dict):
                return schema(**to_dict())

            try:
                payload = json.loads(json.dumps(obj, default=str))
                if isinstance(payload, dict):
                    return schema(**payload)
            except Exception:
                pass

            raise TypeError(f"Unexpected response type: {type(obj)}")

        last_error: Optional[Exception] = None
        total_attempts = int(getattr(self, "max_retries", 2)) + 1  # 1 основная + retries

        # Инициализация structured LLM
        try:
            structured_llm = self._get_structured_llm(schema)  # должен настраивать strict=True/method="function_calling"
            print(f"[{request_id}] _invoke_with_retry: GOT structured_llm={type(structured_llm)}", flush=True)
        except Exception as e:
            logger.exception(f"[{request_id}] {operation}: failed to init structured LLM")
            return ErrorResponse(error=f"{operation}: init structured LLM failed: {e}")

        # Некоторые версии LC не требуют/не поддерживают with_config
        try:
            runnable = structured_llm.with_config(
                callbacks=getattr(self, "callbacks", None)
            )
            print(f"[{request_id}] _invoke_with_retry: runnable={type(runnable)}", flush=True)
        except Exception:
            runnable = structured_llm

        for attempt in range(1, total_attempts + 1):
            try:
                logger.info(f"[{request_id}] {operation}: попытка {attempt}/{total_attempts}")
                print(f"[{request_id}] _invoke_with_retry: INVOKE attempt={attempt}", flush=True)
                res = runnable.invoke(messages)  # должен вернуть schema или dict
                print(f"[{request_id}] _invoke_with_retry: RAW type={type(res)} val={res}", flush=True)
                model_obj = _coerce_to_schema(res)

                # Доп. бизнес-валидация (может бросить исключение)
                try:
                    self._validate_business_rules(model_obj, operation)
                except Exception as be:
                    raise be

                logger.info(f"[{request_id}] {operation}: успех на попытке {attempt}")
                print(f"[{request_id}] _invoke_with_retry: SUCCESS attempt={attempt}", flush=True)
                return model_obj

            except Exception as e:
                last_error = e
                logger.warning(
                    f"[{request_id}] {operation}: попытка {attempt} провалилась — {type(e).__name__}: {e}"
                )
                if attempt < total_attempts:
                    logger.info(f"[{request_id}] {operation}: повторяем...")
                    continue

        # Все попытки исчерпаны — graceful degradation
        logger.exception(f"[{request_id}] {operation}: all attempts failed. Last error: {last_error}")
        print(f"[{request_id}] _invoke_with_retry: ALL FAILED last={last_error!r}", flush=True)
        try:
            print(f"[{request_id}] _invoke_with_retry: GD EXC {ge!r}", flush=True)
            return self._graceful_degradation(
                request_id=request_id,
                operation=operation,
                error=last_error,
                messages=messages,
            )
        except Exception as ge:
            logger.exception(f"[{request_id}] {operation}: graceful_degradation failed: {ge}")
            return ErrorResponse(error=f"{operation} failed: {last_error or ge}")
    
    def _validate_business_rules(self, response: BaseModel, operation: str) -> None:
        """Дополнительная бизнес-валидация поверх Pydantic-схемы."""
        
        if operation == "classify_intent" and isinstance(response, IntentClassification):
            # target_month должен быть указан для get_plan и submit_corrections
            if response.intent in (
                IntentClassification.model_fields["intent"].annotation.GET_PLAN,
                IntentClassification.model_fields["intent"].annotation.SUBMIT_CORRECTIONS,
            ):
                # Это warning, не блокирующая ошибка
                if response.target_month is None:
                    logger.warning(
                        f"Intent={response.intent.value}, но target_month не определён. "
                        f"Reasoning: {response.reasoning}"
                    )
        
        if operation == "generate_response" and isinstance(response, AgentResponse):
            # Ответ не должен быть слишком коротким при success
            if response.status.value == "success" and len(response.message) < 10:
                raise ValueError(
                    f"Ответ со статусом success слишком короткий: '{response.message}'"
                )
    
    def _graceful_degradation(
        self,
        request_id: str,
        operation: str,
        error: Optional[Exception],
        messages: list,
    ) -> ErrorResponse:
        """Graceful degradation: уведомляем пользователя и разработчика."""

        logger.error(
            f"[{request_id}] {operation}: все попытки исчерпаны. "
            f"Последняя ошибка: {type(error).__name__ if error else 'Unknown'}: {error}"
        )

        # Отправляем уведомление разработчику (синхронно)
        if self.developer_email:
            try:
                send_error_notification_sync(
                    developer_email=self.developer_email,
                    request_id=request_id,
                    operation=operation,
                    error=error,
                    messages_dump=str(messages),
                    traceback_str=traceback.format_exc(),
                )
                logger.info(f"[{request_id}] Уведомление отправлено на {self.developer_email}")
            except Exception as notify_err:
                logger.error(
                    f"[{request_id}] Не удалось отправить уведомление: {notify_err}"
                )

        return ErrorResponse(
            success=False,
            message=(
                "К сожалению, в данный момент я не могу обработать ваш запрос. "
                "Команда разработки уже уведомлена о проблеме. "
                f"ID запроса: {request_id}"
            ),
            request_id=request_id,
            retry_after_seconds=60,
        )

    def parse_approval_decision(
        self,
        message: str,
        available_branches: list[str],
        request_id: Optional[str] = None,
    ) -> ApprovalParse | ErrorResponse:    
        """
        Разобрать решение согласующего (approve_branch | reject_branch | finalize_plan)
        через LLM с structured output.
        Returns: ApprovalParse при успехе, ErrorResponse при graceful degradation.
        """
        if request_id is None:
            request_id = str(uuid.uuid4())
            
        try:
            prompt = APPROVAL_DECISION_PROMPT.format(
                available_branches=", ".join(available_branches) if available_branches else "—",
                message=message,
            )
            print(f"[{request_id}] parse_approval_decision: PROMPT OK len={len(prompt)}", flush=True)
            messages = [
                SystemMessage(content=SYSTEM_PROMPT),
                HumanMessage(content=prompt),
            ]
            print(f"[{request_id}] parse_approval_decision: MESSAGES OK types={[type(m) for m in messages]}", flush=True)
        except Exception as e:
            print(f"[{request_id}] parse_approval_decision: PREP ERROR {e!r}\n{traceback.format_exc()}", flush=True)
            return ErrorResponse(error=f"prep_failed: {e}")

        try:
            print(f"[{request_id}] parse_approval_decision: CALL _invoke_with_retry", flush=True)
            return self._invoke_with_retry(
                schema=ApprovalParse,
                messages=messages,
                request_id=request_id,
                operation="parse_approval_decision",
            )
            print(f"[{request_id}] parse_approval_decision: GOT type={type(res)} val={res}", flush=True)
        except Exception as e:
            logger.exception(f"[{request_id}] parse_approval_decision failed")
            return ErrorResponse(error=str(e))
                     