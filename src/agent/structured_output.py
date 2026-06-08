"""Модуль structured output с retry и graceful degradation."""

import uuid
import logging
import traceback
from typing import Optional, Type

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel

from src.agent.schemas import (
    IntentClassification,
    AgentResponse,
    ErrorResponse,
)
from src.agent.notifications import send_error_notification_sync
from src.agent.prompts import CLASSIFY_INTENT_PROMPT, RESPONSE_PROMPT, SYSTEM_PROMPT
from src.config import get_llm, callbacks

logger = logging.getLogger(__name__)


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
        structured_llm = self._get_structured_llm(schema)
        runnable = structured_llm.with_config(callbacks=callbacks if callbacks else None)
        last_error: Optional[Exception] = None
        
        total_attempts = self.max_retries + 1  # 1 основная + 2 retry
        
        for attempt in range(total_attempts):
            try:
                logger.info(
                    f"[{request_id}] {operation}: попытка {attempt + 1}/{total_attempts}"
                )
                
                response = runnable.invoke(messages)
                
                # Дополнительная бизнес-валидация
                self._validate_business_rules(response, operation)
                
                logger.info(f"[{request_id}] {operation}: успех на попытке {attempt + 1}")
                return response
                
            except Exception as e:
                last_error = e
                logger.warning(
                    f"[{request_id}] {operation}: попытка {attempt + 1} провалилась — "
                    f"{type(e).__name__}: {e}"
                )
                
                if attempt < self.max_retries:
                    logger.info(f"[{request_id}] Повторяем...")
                    continue
        
        # Все попытки исчерпаны → graceful degradation
        return self._graceful_degradation(
            request_id=request_id,
            operation=operation,
            error=last_error,
            messages=messages,
        )
    
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