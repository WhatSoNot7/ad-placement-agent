"""Конфигурация LLM."""

import os
from dotenv import load_dotenv

from langfuse.langchain import CallbackHandler
import logging

load_dotenv()

callbacks = []
logger = logging.getLogger(__name__)

# Initialize Langfuse client
handler = CallbackHandler()
callbacks.append(handler)
logger.info("Langfuse callback инициализирован")

def get_llm():
    """Возвращает LLM на основе переменных окружения."""
    provider = os.getenv("LLM_PROVIDER", "openai")

    if provider == "openai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            model=os.getenv("LLM_MODEL", "gpt-4o-mini"),
            api_key=os.getenv("OPENAI_API_KEY"),
            temperature=0,
	    callbacks=callbacks
        )

    elif provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(
            model=os.getenv("LLM_MODEL", "claude-3-5-sonnet-20241022"),
            api_key=os.getenv("ANTHROPIC_API_KEY"),
            temperature=0,
	    callbacks=callbacks
        )

    elif provider == "openai_compatible":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            model=os.getenv("LLM_MODEL", "glm-5"),
            api_key=os.getenv("OPENAI_API_KEY"),
            base_url=os.getenv("LLM_BASE_URL"),
            temperature=0,
	    callbacks=callbacks
        )

    else:
        raise ValueError(f"Unknown LLM_PROVIDER: {provider}")