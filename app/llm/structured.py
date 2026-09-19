"""Structured-output service (Phase 3).

Pipeline::

    messages
    → LLMClient.chat(json_mode=...)     provider-neutral call
    → LLMResponse.content               raw text
    → json.loads()                      layer 1: JSON syntax
    → Pydantic model_validate()         layer 2: schema/semantics
    → StructuredLLMResult               validated data + raw metadata

The two failure layers are deliberately distinct and observable:
- invalid JSON  → :class:`StructuredOutputError` ("not valid JSON …")
- valid JSON that fails schema validation → :class:`StructuredOutputError`
  ("schema validation failed …")

Phase 3 performs **no** repair and **no** automatic retry: if the model
output is broken, the caller sees exactly what was broken. Repair/retry may
be added later only if real usage shows it is needed.

No regex "JSON fixing" (fence stripping, quote replacement, brace
completion) is done on purpose — with the API's verified json_object mode
plus a strict prompt, silent repairs would only hide model failures.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Generic, TypeVar

from pydantic import BaseModel, ValidationError

from app.llm.base import LLMClient, LLMError
from app.models.schemas import LLMResponse
from app.models.structured import SearchIntent
from app.prompts.search_intent import SEARCH_INTENT_SYSTEM_PROMPT

T = TypeVar("T", bound=BaseModel)

_CONTENT_PREVIEW_CHARS = 120
_MAX_VALIDATION_ERRORS_SHOWN = 5

#: Deterministic extraction benefits from low temperature; this single
#: documented default keeps the choice out of scattered call sites.
DEFAULT_EXTRACTION_TEMPERATURE = 0.1


class StructuredOutputError(LLMError):
    """The model output could not be converted into the requested schema.

    The message always says which layer failed — "not valid JSON" or
    "schema validation failed" — plus a short content preview / the
    offending fields. No repair is attempted (Phase 3 policy).
    """


@dataclass
class StructuredLLMResult(Generic[T]):
    """Validated schema instance together with the raw LLM response.

    Kept as a wrapper (instead of returning only the model instance)
    because model name, finish_reason and token usage are needed for the
    CLI display and the saved JSON — dropping them would make live
    behaviour unobservable. ``data`` is the trusted object; nothing in
    ``raw_response`` (e.g. hidden reasoning) is ever persisted.
    """

    data: T
    raw_response: LLMResponse


def chat_structured(
    client: LLMClient,
    messages: list[dict[str, str]],
    schema: type[T],
    *,
    model: str | None = None,
    temperature: float | None = None,
    json_mode: bool = True,
) -> StructuredLLMResult[T]:
    """Chat, then validate the reply as ``schema``.

    Args:
        client: any :class:`~app.llm.base.LLMClient` implementation.
        messages: validated by the client.
        schema: a Pydantic ``BaseModel`` subclass used for validation.
        model / temperature: forwarded to ``client.chat``.
        json_mode: default ``True`` — ask the provider for a JSON object
            where supported (see ``app/llm/glm.py``). Validation below
            never trusts this hint.

    Raises:
        StructuredOutputError: invalid JSON or schema validation failed.
    """
    response = client.chat(
        messages, model=model, temperature=temperature, json_mode=json_mode
    )
    try:
        payload = json.loads(response.content)
    except json.JSONDecodeError as exc:
        raise StructuredOutputError(
            f"Model output is not valid JSON ({exc.msg} at line {exc.lineno} "
            f"column {exc.colno}); content preview: "
            f"{_preview(response.content)}"
        ) from exc
    try:
        data = schema.model_validate(payload)
    except ValidationError as exc:
        raise StructuredOutputError(
            f"schema validation failed for {schema.__name__}: "
            f"{_format_validation_error(exc)}"
        ) from exc
    return StructuredLLMResult(data=data, raw_response=response)


def extract_search_intent(
    client: LLMClient,
    text: str,
    *,
    model: str | None = None,
    temperature: float | None = DEFAULT_EXTRACTION_TEMPERATURE,
    json_mode: bool = True,
) -> StructuredLLMResult[SearchIntent]:
    """Extract a :class:`SearchIntent` from free-form user text.

    This is extraction, not interpretation: the prompt forbids the model
    from adding biology the user did not state. ``temperature`` defaults to
    0.1 (deterministic extraction); pass ``None`` for the API default.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text must be a non-empty string")
    messages = [
        {"role": "system", "content": SEARCH_INTENT_SYSTEM_PROMPT},
        {"role": "user", "content": text},
    ]
    return chat_structured(
        client,
        messages,
        SearchIntent,
        model=model,
        temperature=temperature,
        json_mode=json_mode,
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _preview(content: str) -> str:
    preview = content[:_CONTENT_PREVIEW_CHARS].replace("\n", " ")
    ellipsis = "..." if len(content) > _CONTENT_PREVIEW_CHARS else ""
    return f"{preview!r}{ellipsis}"


def _format_validation_error(exc: ValidationError) -> str:
    details = []
    for error in exc.errors()[:_MAX_VALIDATION_ERRORS_SHOWN]:
        location = ".".join(str(part) for part in error["loc"]) or "(root)"
        details.append(f"{location}: {error['msg']}")
    suffix = "" if len(exc.errors()) <= _MAX_VALIDATION_ERRORS_SHOWN else " …"
    return "; ".join(details) + suffix
