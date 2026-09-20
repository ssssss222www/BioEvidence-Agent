"""Provider-neutral LLM interface (Phase 2, extended in Phase 4).

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

#: Roles allowed in a message list (Phase 4 adds the ``tool`` role).
VALID_ROLES = frozenset({"system", "user", "assistant", "tool"})


class LLMError(RuntimeError):
    """Base class for all LLM-layer failures (user-readable messages)."""


class LLMConfigurationError(LLMError):
    """Missing/invalid API key or model — fix the configuration, not the code."""


class LLMRequestError(LLMError):
    """The request could not be completed or returned an unusable body:
    network/timeout failures, rate limits, other SDK request errors, or
    responses with neither text content nor tool calls.

    Authentication problems — a missing key detected at construction *or*
    a 401 rejection at request time — are always
    :class:`LLMConfigurationError` in the current implementation, because
    both mean the credentials (not the request) must be fixed."""


# ---------------------------------------------------------------------------
# Message contract (Phase 4)
# ---------------------------------------------------------------------------

def validate_messages(messages: object) -> list[dict]:
    """Validate and normalize a chat message list.

    Messages stay plain dicts — the JSON-shaped, provider-neutral
    representation every OpenAI-style API already understands. Four shapes
    are legal (validated per role, unknown keys dropped from the
    normalized copy):

    .. code-block:: text

        {"role": "system"|"user", "content": str}          # non-empty text
        {"role": "assistant", "content": str}              # plain answer
        {"role": "assistant", "content": None|str,         # tool-call turn
         "tool_calls": [{"id", "name", "arguments"}]}      # >= 1 call
        {"role": "tool", "tool_call_id": str, "content": str}  # tool result

    Rules:
    - ``messages`` must be a non-empty list of dicts;
    - ``role`` must be one of :data:`VALID_ROLES`;
    - system/user ``content``: non-empty string;
    - assistant: non-empty ``content`` **or** a non-empty ``tool_calls``
      list (both allowed); each tool call needs non-empty ``id``/``name``
      and a string ``arguments`` (raw JSON text — parsing is the tool
      layer's job, not the message layer's);
    - tool messages require a non-empty ``tool_call_id`` and non-empty
      ``content`` (the serialized tool result);
    - an assistant tool-call turn keeps its ``id``s verbatim so tool
      results can be paired with them (pairing itself is enforced by the
      agent loop, which constructs these messages).

    Raises:
        ValueError: with the offending index and reason.
    """
    if not isinstance(messages, list):
        raise ValueError(
            f"messages must be a list of dicts, got {type(messages).__name__}"
        )
    if not messages:
        raise ValueError("messages must not be empty")
    validated: list[dict] = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise ValueError(
                f"messages[{index}] must be a dict, got {type(message).__name__}"
            )
        role = message.get("role")
        if role not in VALID_ROLES:
            raise ValueError(
                f"messages[{index}] has invalid role {role!r}; "
                f"allowed roles: {sorted(VALID_ROLES)}"
            )
        if role in ("system", "user"):
            validated.append(_text_message(message, index, role))
        elif role == "assistant":
            validated.append(_assistant_message(message, index))
        else:  # role == "tool"
            validated.append(_tool_message(message, index))
    return validated


def _text_message(message: dict, index: int, role: str) -> dict:
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError(
            f"messages[{index}] ({role}) content must be a non-empty string"
        )
    return {"role": role, "content": content}


def _assistant_message(message: dict, index: int) -> dict:
    content = message.get("content")
    if content is not None and not (isinstance(content, str) and content.strip()):
        raise ValueError(
            f"messages[{index}] (assistant) content must be a non-empty "
            "string or null (when tool_calls are present)"
        )
    normalized: dict = {"role": "assistant", "content": content}
    tool_calls = message.get("tool_calls")
    if tool_calls is not None:
        if not isinstance(tool_calls, list) or not tool_calls:
            raise ValueError(
                f"messages[{index}] (assistant) tool_calls must be a "
                "non-empty list when present"
            )
        normalized["tool_calls"] = [
            _tool_call_dict(tool_call, index, position)
            for position, tool_call in enumerate(tool_calls)
        ]
    if normalized["content"] is None and "tool_calls" not in normalized:
        raise ValueError(
            f"messages[{index}] (assistant) needs non-empty content or "
            "a non-empty tool_calls list"
        )
    return normalized


def _tool_call_dict(tool_call: object, index: int, position: int) -> dict:
    if not isinstance(tool_call, dict):
        raise ValueError(
            f"messages[{index}] tool_calls[{position}] must be a dict "
            f"with keys id/name/arguments"
        )
    call_id = tool_call.get("id")
    name = tool_call.get("name")
    arguments = tool_call.get("arguments")
    if not isinstance(call_id, str) or not call_id.strip():
        raise ValueError(
            f"messages[{index}] tool_calls[{position}] needs a non-empty id"
        )
    if not isinstance(name, str) or not name.strip():
        raise ValueError(
            f"messages[{index}] tool_calls[{position}] needs a non-empty name"
        )
    if not isinstance(arguments, str):
        raise ValueError(
            f"messages[{index}] tool_calls[{position}] arguments must be "
            "a string (raw JSON from the provider)"
        )
    return {"id": call_id, "name": name, "arguments": arguments}


def _tool_message(message: dict, index: int) -> dict:
    call_id = message.get("tool_call_id")
    content = message.get("content")
    if not isinstance(call_id, str) or not call_id.strip():
        raise ValueError(
            f"messages[{index}] (tool) requires a non-empty tool_call_id "
            "matching the assistant tool call it answers"
        )
    if not isinstance(content, str) or not content.strip():
        raise ValueError(
            f"messages[{index}] (tool) content must be a non-empty string "
            "(the serialized tool result)"
        )
    return {"role": "tool", "tool_call_id": call_id, "content": content}


# ---------------------------------------------------------------------------
# Client protocol
# ---------------------------------------------------------------------------

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
      (see ``app/llm/structured.py``);
    - ``tools`` (Phase 4) is a list of OpenAI-style tool definitions
      (``{"type": "function", "function": {name, description, parameters}}``).
      ``None`` (the default) preserves the exact Phase 2/3 behaviour — no
      tool parameters are sent;
    - ``tool_choice`` (Phase 4): ``"auto"`` lets the model decide;
      ``"none"`` disables tool use. Provider-specific forcing objects are
      intentionally not part of the neutral contract.

    A provider client translates these neutral shapes to/from its wire
    format and must never execute tools, import business modules, or know
    about the agent loop — protocol conversion only.
    """

    def chat(
        self,
        messages: list[dict],
        *,
        model: str | None = None,
        temperature: float | None = None,
        json_mode: bool = False,
        tools: list[dict] | None = None,
        tool_choice: str | None = None,
    ) -> LLMResponse: ...
