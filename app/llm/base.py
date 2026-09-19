"""Provider-neutral LLM interface (Phase 2).

This module is the only thing upper layers should import from ``app.llm``
besides a concrete client: the error taxonomy, message validation, and the
:class:`LLMClient` interface. It contains no SDK imports, so code written
against it stays provider-independent.

Design choice — ``typing.Protocol`` instead of ``abc.ABC``: the interface
is structural ("has a ``chat`` method with this shape"). Protocol needs no
inheritance, so a future ``QwenClient`` (or a test double) conforms just by
implementing the method — no base-class import, no ``__subclasshook__``
ceremony. An ABC would add coupling without adding safety here.
"""

from __future__ import annotations

from typing import Protocol

from app.models.schemas import LLMResponse

#: Roles allowed in a Phase 2 message list. Tool/multimodal roles are
#: intentionally excluded — they belong to later phases.
VALID_ROLES = frozenset({"system", "user", "assistant"})


class LLMError(RuntimeError):
    """Base class for all LLM-layer failures (user-readable messages)."""


class LLMConfigurationError(LLMError):
    """Missing/invalid API key or model — fix the configuration, not the code."""


class LLMRequestError(LLMError):
    """The request could not be completed or returned an unusable body:
    network/timeout failures, rate limits, other SDK request errors, or
    responses without ``choices`` / with empty ``content``.

    Authentication problems — a missing key detected at construction *or*
    a 401 rejection at request time — are always
    :class:`LLMConfigurationError` in the current implementation, because
    both mean the credentials (not the request) must be fixed."""


def validate_messages(messages: object) -> list[dict[str, str]]:
    """Validate and normalize a chat message list.

    Phase 2 messages are plain ``list[dict[str, str]]`` (chosen over a
    ChatMessage dataclass because it is exactly the wire format every
    OpenAI-style SDK — including ZhipuAI — already accepts; one fewer
    translation layer, same validation guarantees).

    Rules enforced:
    - ``messages`` must be a non-empty list of dicts;
    - each dict needs ``role`` in :data:`VALID_ROLES` and ``content`` as a
      non-empty string (after strip);
    - unknown keys are dropped from the normalized copy.

    Raises:
        ValueError: with the offending index and reason.
    """
    if not isinstance(messages, list):
        raise ValueError(
            f"messages must be a list of dicts, got {type(messages).__name__}"
        )
    if not messages:
        raise ValueError("messages must not be empty")
    validated: list[dict[str, str]] = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise ValueError(
                f"messages[{index}] must be a dict, got {type(message).__name__}"
            )
        role = message.get("role")
        content = message.get("content")
        if role not in VALID_ROLES:
            raise ValueError(
                f"messages[{index}] has invalid role {role!r}; "
                f"allowed roles: {sorted(VALID_ROLES)}"
            )
        if not isinstance(content, str) or not content.strip():
            raise ValueError(
                f"messages[{index}] ({role}) content must be a non-empty string"
            )
        validated.append({"role": role, "content": content})
    return validated


class LLMClient(Protocol):
    """Structural interface every provider client must satisfy.

    ``chat`` takes a validated message list plus optional per-call overrides
    and returns a provider-neutral :class:`LLMResponse`:

    - ``model`` falls back to the client's configured default;
    - ``temperature`` ``None`` means "let the API use its own default";
    - ``json_mode=True`` asks the provider to constrain the reply to a
      JSON object via its native mechanism (e.g. ``response_format``).
      It is a *request hint only* — it never validates the reply; callers
      that need valid data must parse/validate the content themselves
      (see ``app/llm/structured.py``).
    """

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float | None = None,
        json_mode: bool = False,
    ) -> LLMResponse: ...
