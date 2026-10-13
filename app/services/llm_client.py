"""LLM client abstraction.

Workers and the Supervisor's decider depend on the small
``StructuredLLM`` protocol — one structured-output call. Production uses
``ChatOpenAI.with_structured_output``; without credentials a deterministic
``StubLLM`` keeps the whole system runnable offline and in CI.
"""

from collections.abc import Callable
from typing import Any, Protocol, TypeVar, runtime_checkable

from langchain_core.runnables import RunnableConfig
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

    def __init__(self, settings: Settings, *, model: str | None = None) -> None:
        from langchain_openai import ChatOpenAI

        self._model = ChatOpenAI(
            model=model or settings.chat_model_name,
            api_key=settings.openai_api_key,
            # Transient 429/5xx retry with exponential backoff — parallel jobs
            # can briefly saturate the org TPM limit mid-run.
            max_retries=6,
        )
        # Bounded outputs: schemas already cap completion size; the setting
        # is an extra seatbelt when configured (None = provider default).
        if settings.llm_max_tokens is not None:
            self._model.max_tokens = settings.llm_max_tokens

    async def structured(
        self,
        schema: type[_T],
        *,
        system: str,
        user: str,
        context: dict[str, Any] | None = None,
    ) -> _T:
        # include_raw gives us the AIMessage — its usage_metadata carries
        # the provider's real token counts, which tracked_structured
        # prefers over the chars/4 estimate.
        runnable = self._model.with_structured_output(schema, include_raw=True)
        # Prompt provenance rides on the run config: LangSmith shows it as
        # run metadata + a `prompt:<name>@v<N>` tag when tracing is enabled;
        # it is inert otherwise (no behavior change offline).
        config: RunnableConfig | None = None
        if context and context.get("prompt_name"):
            config = {
                "metadata": {
                    "prompt_name": context.get("prompt_name"),
                    "prompt_version": context.get("prompt_version"),
                    "prompt_sha256": context.get("prompt_sha256"),
                },
                "tags": [
                    f"prompt:{context['prompt_name']}"
                    f"@v{context.get('prompt_version', '?')}"
                ],
            }
        out = await runnable.ainvoke(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            config=config,
        )
        usage = getattr(out.get("raw"), "usage_metadata", None)
        if context is not None and isinstance(usage, dict):
            context["token_usage"] = {
                "prompt_tokens": usage.get("input_tokens"),
                "completion_tokens": usage.get("output_tokens"),
            }
        return schema.model_validate(out.get("parsed"))


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

    def register_default(self, schema_name: str, builder: StubBuilder) -> None:
        """Register only when absent — caller-supplied builders win."""
        self._builders.setdefault(schema_name, builder)

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


def build_llm_overrides(settings: Settings) -> dict[str, StructuredLLM]:
    """Per-role LLM overrides (latency candidate): e.g. ``REVIEWER_MODEL``
    routes only the reviewer's calls to a cheaper/faster checkpoint.
    Offline (stub) mode yields no overrides — the stub is already free.
    """
    if settings.openai_api_key is None:
        return {}
    overrides: dict[str, StructuredLLM] = {}
    if settings.reviewer_model:
        overrides["reviewer"] = OpenAIStructuredLLM(
            settings, model=settings.reviewer_model
        )
    return overrides


def build_judge_llm(settings: Settings) -> StructuredLLM:
    """Factory for the LLM-as-judge — a different model than the generator.

    Falls back to the deterministic stub offline; the eval registers a
    ``JudgeOutput`` builder on it.
    """
    if settings.openai_api_key is not None:
        return OpenAIStructuredLLM(settings, model=settings.judge_model)
    return StubLLM()
