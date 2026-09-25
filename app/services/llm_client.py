"""LLM client abstraction.

Workers and the Supervisor's decider depend on the small
``StructuredLLM`` protocol — one structured-output call. Production uses
``ChatOpenAI.with_structured_output``; without credentials a deterministic
``StubLLM`` keeps the whole system runnable offline and in CI.
"""

from collections.abc import Callable
from typing import Any, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

from app.core.config import Settings

_T = TypeVar("_T", bound=BaseModel)


@runtime_checkable
class StructuredLLM(Protocol):
    """Minimal structured-output interface every agent consumes."""

    async def structured(
        self,
        schema: type[_T],
        *,
        system: str,
        user: str,
        context: dict[str, Any] | None = None,
    ) -> _T:
        """Return a validated ``schema`` instance for the given prompt."""
        ...


class OpenAIStructuredLLM:
    """``StructuredLLM`` backed by langchain-openai structured output."""

    def __init__(self, settings: Settings) -> None:
        from langchain_openai import ChatOpenAI

        self._model = ChatOpenAI(
            model=settings.chat_model_name,
            api_key=settings.openai_api_key,
        )

    async def structured(
        self,
        schema: type[_T],
        *,
        system: str,
        user: str,
        context: dict[str, Any] | None = None,
    ) -> _T:
        runnable = self._model.with_structured_output(schema)
        result = await runnable.ainvoke(
            [{"role": "system", "content": system}, {"role": "user", "content": user}]
        )
        return schema.model_validate(result)


# Builders produce deterministic, schema-valid outputs for offline/stub mode.
StubBuilder = Callable[[type[BaseModel], dict[str, Any] | None], BaseModel]


class StubLLM:
    """Deterministic offline LLM.

    Builders are keyed by schema class name; unknown schemas fall back to
    ``model_construct`` with field defaults, which is enough for graphs to
    run end-to-end without credentials.
    """

    def __init__(self, builders: dict[str, StubBuilder] | None = None) -> None:
        self._builders = builders or {}

    def register(self, schema_name: str, builder: StubBuilder) -> None:
        self._builders[schema_name] = builder

    async def structured(
        self,
        schema: type[_T],
        *,
        system: str,
        user: str,
        context: dict[str, Any] | None = None,
    ) -> _T:
        builder = self._builders.get(schema.__name__)
        if builder is not None:
            return schema.model_validate(builder(schema, context))
        return schema.model_construct()


def build_llm(settings: Settings) -> StructuredLLM:
    """Factory: real OpenAI when a key exists, deterministic stub otherwise."""
    if settings.openai_api_key is not None:
        return OpenAIStructuredLLM(settings)
    return StubLLM()
