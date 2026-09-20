"""GLM chat client over the official ZhipuAI SDK (Phase 2).

Responsibilities::

    messages
    → validate_messages()            (provider-neutral validation)
    → ZhipuAI SDK  chat.completions.create(stream=False)
    → raw SDK response
    → _normalize_response()          (attribute-tolerant extraction)
    → LLMResponse                    (project-wide data contract)

Retry policy: the official SDK already retries transient HTTP failures
itself (``max_retries`` constructor argument, default 3). We pin it to 2
and implement **no** additional retry loop — stacking retries multiplies
latency and can turn a rate limit into a self-DDoS. Authentication
failures are never retried by us and surface immediately.

Model resolution: ``chat(model=...)`` → client default (``GLM_MODEL`` env
or constructor argument) → :class:`LLMConfigurationError`.

Temperature: ``None`` (the default) omits the parameter entirely so the
GLM API applies its own default; an explicit value must lie in ``[0, 1]``.

``reasoning_content`` (hidden chain-of-thought on thinking models) is
deliberately neither read nor exposed.
"""

from __future__ import annotations

import os

import zhipuai
from zhipuai import ZhipuAI

from app.config import load_env_file
from app.llm.base import (
    LLMConfigurationError,
    LLMError,
    LLMRequestError,
    validate_messages,
)
from app.models.schemas import LLMResponse, ToolCall

DEFAULT_TIMEOUT_SECONDS = 60.0
SDK_MAX_RETRIES = 2  # rely on the SDK's built-in retry, nothing on top
API_KEY_ENV = "GLM_API_KEY"
MODEL_ENV = "GLM_MODEL"

# JSON-mode capability, verified with a real minimal request on 2026-09-19:
# zhipuai SDK v2.1.5 + glm-5.3 accept response_format={"type": "json_object"}
# and return a parseable JSON object (see docs/phase-03-structured-output.md).
# If a future model rejects the parameter, set this to False — json_mode then
# relies on the prompt alone instead of failing requests.
GLM_JSON_MODE_SUPPORTED = True


class GLMClient:
    """Thin, provider-specific adapter implementing the :class:`LLMClient`
    protocol (structurally — see ``app/llm/base.py`` for why there is no
    base class)."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        load_env_file()
        self._api_key = (api_key or os.environ.get(API_KEY_ENV, "")).strip()
        if not self._api_key:
            raise LLMConfigurationError(
                f"{API_KEY_ENV} is not configured: set it in the environment "
                "or in a project-root .env file (see .env.example)"
            )
        self._default_model = (
            (model or os.environ.get(MODEL_ENV, "")).strip() or None
        )
        try:
            self._client = ZhipuAI(
                api_key=self._api_key,
                timeout=timeout,
                max_retries=SDK_MAX_RETRIES,
            )
        except Exception as exc:  # SDK init should not fail, but be explicit
            raise LLMConfigurationError(
                f"Could not initialize the ZhipuAI SDK client: {_redact(str(exc), self._api_key)}"
            ) from exc

    @property
    def default_model(self) -> str | None:
        """Model used when ``chat()`` gets no explicit ``model``."""
        return self._default_model

    def chat(
        self,
        messages: list[dict],
        *,
        model: str | None = None,
        temperature: float | None = None,
        json_mode: bool = False,
        tools: list[dict] | None = None,
        tool_choice: str | None = None,
    ) -> LLMResponse:
        """One non-streaming chat completion against the GLM API.

        ``json_mode=True`` adds the provider-native
        ``response_format={"type": "json_object"}`` parameter — a hint the
        API constrain its reply to a JSON object. It does not validate the
        reply; callers wanting trustworthy data use
        ``app/llm/structured.py``.

        Phase 4: ``tools`` is a list of OpenAI-style tool definitions;
        ``tool_choice="auto"`` lets the model decide. Neutral assistant
        tool-call / tool-result messages are translated to ZhipuAI's wire
        format, and ZhipuAI ``tool_calls`` are parsed into neutral
        :class:`ToolCall` records. This method never executes tools.
        """
        messages = validate_messages(messages)
        resolved_model = (model or self._default_model or "").strip()
        if not resolved_model:
            raise LLMConfigurationError(
                f"No GLM model configured: pass model=... to chat() or set "
                f"{MODEL_ENV} (see .env.example)"
            )
        request: dict = {
            "model": resolved_model,
            "messages": _to_provider_messages(messages),
            "stream": False,  # fixed in Phase 2 to keep the flow simple
        }
        if temperature is not None:
            request["temperature"] = _validated_temperature(temperature)
        if json_mode and GLM_JSON_MODE_SUPPORTED:
            request["response_format"] = {"type": "json_object"}
        # json_mode=True with GLM_JSON_MODE_SUPPORTED == False intentionally
        # sends nothing: the prompt must then carry the JSON instruction.
        if tools is not None:
            if not tools:
                raise ValueError("tools must be a non-empty list when provided")
            request["tools"] = tools
            if tool_choice is not None:
                if tool_choice not in ("auto", "none"):
                    raise ValueError(
                        f"tool_choice must be 'auto' or 'none', got {tool_choice!r}"
                    )
                request["tool_choice"] = tool_choice
        try:
            raw = self._client.chat.completions.create(**request)
        except zhipuai.ZhipuAIError as exc:
            raise self._sdk_error(exc) from exc
        except Exception as exc:  # httpx/transport-level surprises
            raise LLMRequestError(
                f"GLM request failed unexpectedly "
                f"({type(exc).__name__}): {_redact(str(exc), self._api_key)}"
            ) from exc
        return _normalize_response(raw, resolved_model)

    # ------------------------------------------------------------------
    # error translation
    # ------------------------------------------------------------------

    def _sdk_error(self, exc: Exception) -> LLMError:
        """Map a ZhipuAI SDK exception onto the project's error taxonomy."""
        detail = _redact(str(exc), self._api_key)
        if isinstance(exc, zhipuai.APIAuthenticationError):
            return LLMConfigurationError(
                f"GLM authentication failed (check {API_KEY_ENV}): {detail}"
            )
        if isinstance(
            exc, (zhipuai.APIReachLimitError, zhipuai.APIServerFlowExceedError)
        ):
            # APIReachLimitError also covers quota/balance errors such as
            # error code 1113 ("余额不足"), observed in real use.
            return LLMRequestError(f"GLM rate limit or quota exceeded: {detail}")
        if isinstance(exc, (zhipuai.APIConnectionError, zhipuai.APITimeoutError)):
            return LLMRequestError(f"GLM network error: {detail}")
        return LLMRequestError(
            f"GLM request failed ({type(exc).__name__}): {detail}"
        )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _validated_temperature(temperature: float) -> float:
    if (
        isinstance(temperature, bool)
        or not isinstance(temperature, (int, float))
        or not 0.0 <= float(temperature) <= 1.0
    ):
        raise ValueError(
            f"temperature must be a number in [0, 1], got {temperature!r}"
        )
    return float(temperature)


def _redact(text: str, secret: str) -> str:
    """Replace any occurrence of the API key so it can never leak into
    exception messages or logs."""
    if secret and secret in text:
        return text.replace(secret, "***REDACTED***")
    return text


def _normalize_response(raw: object, requested_model: str) -> LLMResponse:
    """Extract an :class:`LLMResponse` from a raw SDK response object.

    Uses ``getattr`` throughout because providers/models vary in which
    attributes they populate. A response must yield at least one of
    non-empty text content or a non-empty tool-call list; anything else is
    an unusable body.
    """
    choices = getattr(raw, "choices", None)
    if not choices:
        raise LLMRequestError(
            f"GLM response contains no choices: {_summarize(raw)}"
        )
    choice = choices[0]
    message = getattr(choice, "message", None)
    content = getattr(message, "content", None) if message is not None else None
    if not isinstance(content, str) or not content.strip():
        content = None  # e.g. tool-call-only answers carry content=None
    tool_calls = _parse_provider_tool_calls(message)
    if content is None and not tool_calls:
        raise LLMRequestError(
            "GLM response has empty assistant content and no tool_calls "
            f"(finish_reason={getattr(choice, 'finish_reason', None)!r})"
        )
    usage = getattr(raw, "usage", None)

    def usage_token(name: str) -> int | None:
        value = getattr(usage, name, None)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return int(value)

    return LLMResponse(
        content=content,
        tool_calls=tool_calls,
        model=getattr(raw, "model", None) or requested_model,
        finish_reason=getattr(choice, "finish_reason", None),
        prompt_tokens=usage_token("prompt_tokens"),
        completion_tokens=usage_token("completion_tokens"),
        total_tokens=usage_token("total_tokens"),
    )


def _parse_provider_tool_calls(message: object) -> list[ToolCall]:
    """ZhipuAI ``message.tool_calls`` → neutral :class:`ToolCall` list.

    Provider structure (verified live, SDK v2.1.5 / glm-4-flash, 2026-09-19):
    ``[{id, type: "function", function: {name, arguments: "<json str>"}}]``.
    ``arguments`` stays a raw JSON string — business parsing belongs to the
    tool layer, not the provider adapter.
    """
    raw_calls = getattr(message, "tool_calls", None) if message is not None else None
    if not raw_calls:
        return []
    calls: list[ToolCall] = []
    for raw_call in raw_calls:
        function = getattr(raw_call, "function", None)
        call_id = (getattr(raw_call, "id", None) or "").strip()
        name = (getattr(function, "name", None) or "").strip()
        arguments = getattr(function, "arguments", None)
        if not call_id or not name or not isinstance(arguments, str):
            raise LLMRequestError(
                "GLM tool_calls entry is missing id, name, or string "
                f"arguments: {_summarize(raw_call)}"
            )
        calls.append(ToolCall(id=call_id, name=name, arguments=arguments))
    return calls


def _to_provider_messages(messages: list[dict]) -> list[dict]:
    """Neutral message dicts → ZhipuAI wire format.

    Differences from the neutral form: assistant tool calls use ZhipuAI's
    nested ``{"id", "type": "function", "function": {name, arguments}}``
    shape. System/user/assistant-text/tool messages pass through unchanged
    (already wire-compatible). Unknown keys were already dropped by
    ``validate_messages``.
    """
    provider_messages: list[dict] = []
    for message in messages:
        if message["role"] == "assistant" and "tool_calls" in message:
            provider_messages.append(
                {
                    "role": "assistant",
                    "content": message["content"],
                    "tool_calls": [
                        {
                            "id": call["id"],
                            "type": "function",
                            "function": {
                                "name": call["name"],
                                "arguments": call["arguments"],
                            },
                        }
                        for call in message["tool_calls"]
                    ],
                }
            )
        else:
            provider_messages.append(dict(message))
    return provider_messages


def _summarize(raw: object) -> str:
    """Short, secret-free representation of an unexpected response object."""
    text = repr(raw)
    return text[:200] + ("..." if len(text) > 200 else "")
