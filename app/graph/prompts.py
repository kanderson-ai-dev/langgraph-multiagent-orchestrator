"""Prompt compatibility shim.

All agent system prompts now live in the versioned Prompt Registry
(``prompts/<name>/v<N>.yaml``, served by ``app.services.prompt_registry``)
and are injected into every call site — no prompt constants remain here.

Security invariant baked into every worker prompt: fetched web/document
content is *data*, never instructions (OWASP LLM01 indirect injection).
"""

HUMAN_REVIEW_REASON = (
    "Debate budget exhausted without approval — escalating to human review."
)
