"""Guardrails — input screening, untrusted-content sanitization, output check.

Three independent gates (OWASP LLM01/LLM02):

1. ``screen_input`` — runs on the user-supplied brief *before* any paid call.
2. ``sanitize_untrusted`` — runs on scraped/parsed content before it may be
   embedded in a prompt (indirect-injection defense).
3. ``screen_output`` — runs on the final report before it ships.
"""

import re
import unicodedata
from dataclasses import dataclass

# Injection / jailbreak signals on user input (compiled once).
_INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?(previous|prior|above|earlier)\s+instructions", re.I),
    re.compile(r"(reveal|show|print|repeat)\s+(your|the)\s+(system\s+)?prompt", re.I),
    re.compile(r"you\s+are\s+now\s+(a|an|in)\b", re.I),
    re.compile(r"\bDAN\b|jailbreak|do\s+anything\s+now", re.I),
    re.compile(r"<\s*(system|instruction|prompt)\s*>", re.I),
    re.compile(r"exfiltrat|leak\s+(api\s+)?keys?|reveal\s+secrets?", re.I),
    re.compile(r"base64|rot13\s+(decode|encode|this)", re.I),
]

# Phrases embedded in hostile pages that look like instructions to the model.
_INSTRUCTION_PHRASES = re.compile(
    r"(?i)(ignore\s+(all\s+)?(previous|prior)\s+instructions|"
    r"you\s+(must|should|are\s+required\s+to)\s+(now\s+)?(output|reveal|say)|"
    r"as\s+an?\s+ai\s+assistant,?\s+(you|i)\s+will|"
    r"new\s+instructions?:|override\s+(system|previous)\s+prompt)"
)

_OUTPUT_LEAK_PATTERNS = [
    re.compile(r"(?i)my\s+(system\s+)?prompt\s+(is|was|says)"),
    re.compile(r"(?i)you\s+are\s+the\s+(supervisor|researcher|writer|reviewer)\s+agent"),
    re.compile(r"(?i)here\s+is\s+my\s+(system|instruction)\s+prompt"),
]


@dataclass(frozen=True)
class GuardrailResult:
    allowed: bool
    reason: str = ""


def screen_input(text: str) -> GuardrailResult:
    """Screen raw user input. Blocked input never reaches the LLM."""
    if not text or not text.strip():
        return GuardrailResult(False, "empty input")
    if len(text) > 10_000:
        return GuardrailResult(False, "input exceeds 10k chars")
    for pat in _INJECTION_PATTERNS:
        m = pat.search(text)
        if m:
            return GuardrailResult(False, f"matched suspicious pattern: {pat.pattern}")
    return GuardrailResult(True)


def sanitize_untrusted(text: str) -> str:
    """Neutralize scraped/parsed content before it reaches a prompt.

    - normalize invisible/zero-width unicode,
    - defang embedded instruction phrases (replaced with a data marker),
    - collapse control characters.
    """
    # Strip zero-width and control chars (keep \n, \t).
    text = "".join(
        ch for ch in unicodedata.normalize("NFKC", text)
        if ch == "\n" or ch == "\t" or unicodedata.category(ch) not in ("Cf", "Cc", "Co")
    )
    # Defang instruction-like phrases so they read as data, not commands.
    text = _INSTRUCTION_PHRASES.sub("[filtered]", text)
    return text.strip()


def screen_output(text: str) -> GuardrailResult:
    """Screen generated output for prompt leakage / reflected injection."""
    for pat in _OUTPUT_LEAK_PATTERNS:
        if pat.search(text):
            return GuardrailResult(False, f"output leakage pattern: {pat.pattern}")
    return GuardrailResult(True)


def screen_brief(brief: object) -> GuardrailResult:
    """Screen the full brief — topic plus any user-supplied fields."""
    from app.graph.state import Brief

    if not isinstance(brief, Brief):
        return GuardrailResult(False, "invalid brief object")
    fields = " ".join(
        [brief.topic, brief.audience, brief.tone, *brief.requirements]
    )
    return screen_input(fields)
