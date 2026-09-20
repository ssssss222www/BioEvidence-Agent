"""Offline tests for the Phase 2 GLM client.

All tests fake the ZhipuAI SDK (no network). The fake is injected by
monkeypatching ``app.llm.glm.ZhipuAI``; ``load_env_file`` is neutralized so
a developer's real project ``.env`` can never leak credentials into the
test environment. One opt-in real-network smoke test sits at the bottom
(marker ``llm_network``, skipped unless GLM_API_KEY/GLM_MODEL exist).
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import httpx
import pytest
import zhipuai

import app.llm.glm as glm_module
import main as main_module
from app.config import load_env_file as _load_env_for_smoke
from app.llm.base import (
    LLMConfigurationError,
    LLMError,
    LLMRequestError,
    validate_messages,
)
from app.llm.glm import GLMClient
from app.models.schemas import LLMResponse

TEST_KEY = "test-key-abcdef123456"
USER = [{"role": "user", "content": "hi"}]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def make_raw_response(
    content: str = "Hello!",
    model: str | None = "glm-test-model",
    finish_reason: str | None = "stop",
    usage: bool = True,
) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content),
                finish_reason=finish_reason,
            )
        ],
        model=model,
        usage=(
            SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15)
            if usage
            else None
        ),
    )


@pytest.fixture
def sdk(monkeypatch):
    """Fake ZhipuAI SDK + isolated environment."""
    state = SimpleNamespace(response=make_raw_response(), error=None, instances=[])

    def fake_factory(**init_kwargs):
        instance = SimpleNamespace(init_kwargs=init_kwargs, requests=[])

        def fake_create(**request):
            instance.requests.append(request)
            if state.error is not None:
                raise state.error
            return state.response

        instance.chat = SimpleNamespace(
            completions=SimpleNamespace(create=fake_create)
        )
        state.instances.append(instance)
        return instance

    monkeypatch.setattr(glm_module, "ZhipuAI", fake_factory)
    monkeypatch.setattr(glm_module, "load_env_file", lambda: None)
    monkeypatch.setenv("GLM_API_KEY", TEST_KEY)
    monkeypatch.setenv("GLM_MODEL", "glm-test")
    return state


# ---------------------------------------------------------------------------
# 1-2. configuration
# ---------------------------------------------------------------------------

def test_missing_api_key_raises_configuration_error(monkeypatch, sdk):
    monkeypatch.delenv("GLM_API_KEY")
    with pytest.raises(LLMConfigurationError, match="GLM_API_KEY"):
        GLMClient()


def test_missing_model_raises_configuration_error(monkeypatch, sdk):
    monkeypatch.delenv("GLM_MODEL")
    client = GLMClient()  # constructing without a model is allowed
    assert client.default_model is None
    with pytest.raises(LLMConfigurationError, match="GLM_MODEL"):
        client.chat(USER)


def test_sdk_init_receives_timeout_and_bounded_retries(sdk):
    GLMClient()
    init = sdk.instances[0].init_kwargs
    assert init["api_key"] == TEST_KEY
    assert init["max_retries"] == glm_module.SDK_MAX_RETRIES
    assert init["timeout"] == glm_module.DEFAULT_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# 3-5. message validation
# ---------------------------------------------------------------------------

def test_empty_messages_rejected(sdk):
    with pytest.raises(ValueError, match="must not be empty"):
        GLMClient().chat([])


def test_invalid_role_rejected(sdk):
    # "tool" became a valid role in Phase 4 (with tool_call_id); use roles
    # that are invalid in any shape here.
    with pytest.raises(ValueError, match=r"invalid role 'developer'"):
        GLMClient().chat([{"role": "developer", "content": "x"}])
    with pytest.raises(ValueError, match=r"invalid role 'function'"):
        GLMClient().chat([{"role": "function", "content": "x"}])


def test_blank_content_rejected(sdk):
    with pytest.raises(ValueError, match="non-empty string"):
        GLMClient().chat([{"role": "user", "content": "   "}])
    with pytest.raises(ValueError, match="non-empty string"):
        GLMClient().chat([{"role": "assistant", "content": ""}])


def test_validate_messages_rejects_non_list():
    with pytest.raises(ValueError, match="must be a list"):
        validate_messages("hi")


def test_validate_messages_rejects_non_dict_element():
    with pytest.raises(ValueError, match=r"messages\[0\] must be a dict"):
        validate_messages(["hi"])


def test_validate_messages_drops_unknown_keys():
    validated = validate_messages(
        [{"role": "user", "content": "hi", "extra": "ignored"}]
    )
    assert validated == [{"role": "user", "content": "hi"}]


# ---------------------------------------------------------------------------
# 6-8. message shapes sent to the SDK
# ---------------------------------------------------------------------------

def test_single_user_message_roundtrip(sdk):
    response = GLMClient().chat(USER)
    assert isinstance(response, LLMResponse)
    assert response.content == "Hello!"
    request = sdk.instances[0].requests[0]
    assert request["messages"] == USER
    assert request["model"] == "glm-test"
    assert request["stream"] is False


def test_system_and_user_messages(sdk):
    messages = [
        {"role": "system", "content": "You are a precise assistant."},
        {"role": "user", "content": "hi"},
    ]
    GLMClient().chat(messages)
    assert sdk.instances[0].requests[0]["messages"] == messages


def test_assistant_history_message(sdk):
    messages = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
    ]
    GLMClient().chat(messages)
    assert sdk.instances[0].requests[0]["messages"] == messages


def test_explicit_model_overrides_default(sdk):
    GLMClient().chat(USER, model="glm-4-flash")
    assert sdk.instances[0].requests[0]["model"] == "glm-4-flash"


# ---------------------------------------------------------------------------
# 9-13. response normalization
# ---------------------------------------------------------------------------

def test_content_extracted_from_choices(sdk):
    assert GLMClient().chat(USER).content == "Hello!"


def test_model_metadata_reported_by_api(sdk):
    assert GLMClient().chat(USER).model == "glm-test-model"


def test_model_falls_back_to_requested_name(sdk):
    sdk.response = make_raw_response(model=None)
    assert GLMClient().chat(USER).model == "glm-test"


def test_finish_reason_parsed(sdk):
    assert GLMClient().chat(USER).finish_reason == "stop"


def test_usage_parsed(sdk):
    response = GLMClient().chat(USER)
    assert (response.prompt_tokens, response.completion_tokens, response.total_tokens) == (10, 5, 15)


def test_usage_missing_allows_none(sdk):
    sdk.response = make_raw_response(usage=False)
    response = GLMClient().chat(USER)
    assert response.prompt_tokens is None
    assert response.completion_tokens is None
    assert response.total_tokens is None


# ---------------------------------------------------------------------------
# 14-15. malformed responses
# ---------------------------------------------------------------------------

def test_missing_choices_raises(sdk):
    sdk.response = SimpleNamespace(choices=None, model="m", usage=None)
    with pytest.raises(LLMRequestError, match="no choices"):
        GLMClient().chat(USER)
    sdk.response = SimpleNamespace(choices=[], model="m", usage=None)
    with pytest.raises(LLMRequestError, match="no choices"):
        GLMClient().chat(USER)


def test_empty_assistant_content_raises(sdk):
    sdk.response = make_raw_response(content="   ")
    with pytest.raises(LLMRequestError, match="empty assistant content"):
        GLMClient().chat(USER)
    sdk.response = make_raw_response(content=None)
    with pytest.raises(LLMRequestError, match="empty assistant content"):
        GLMClient().chat(USER)


# ---------------------------------------------------------------------------
# 16-17. SDK error translation and secret redaction
# ---------------------------------------------------------------------------

def test_sdk_exception_becomes_llm_request_error(sdk):
    sdk.error = zhipuai.ZhipuAIError("boom")
    with pytest.raises(LLMRequestError, match="boom"):
        GLMClient().chat(USER)


def test_sdk_auth_error_becomes_configuration_error(sdk):
    response = httpx.Response(
        401, request=httpx.Request("POST", "https://api.example.com")
    )
    sdk.error = zhipuai.APIAuthenticationError("invalid api key", response=response)
    with pytest.raises(LLMConfigurationError, match="authentication failed"):
        GLMClient().chat(USER)


def test_unexpected_exception_becomes_llm_request_error(sdk):
    sdk.error = RuntimeError("socket exploded")
    with pytest.raises(LLMRequestError, match="socket exploded"):
        GLMClient().chat(USER)


def test_api_key_never_leaks_in_error_messages(sdk):
    sdk.error = zhipuai.ZhipuAIError(f"request failed with key {TEST_KEY}")
    with pytest.raises(LLMError) as excinfo:
        GLMClient().chat(USER)
    assert TEST_KEY not in str(excinfo.value)
    assert "***REDACTED***" in str(excinfo.value)


# ---------------------------------------------------------------------------
# temperature
# ---------------------------------------------------------------------------

def test_temperature_passed_through(sdk):
    GLMClient().chat(USER, temperature=0.3)
    assert sdk.instances[0].requests[0]["temperature"] == 0.3


def test_temperature_omitted_when_none_uses_api_default(sdk):
    GLMClient().chat(USER)
    assert "temperature" not in sdk.instances[0].requests[0]


@pytest.mark.parametrize("bad", [-0.1, 1.5, "hot", True])
def test_invalid_temperature_rejected(sdk, bad):
    with pytest.raises(ValueError, match="temperature"):
        GLMClient().chat(USER, temperature=bad)


# ---------------------------------------------------------------------------
# 18. CLI orchestration
# ---------------------------------------------------------------------------

def test_cli_llm_happy_path(monkeypatch, capsys):
    seen = {}

    class FakeClient:
        def __init__(self, model=None):
            pass

        def chat(self, messages, *, model=None, temperature=None):
            seen["messages"] = messages
            seen["temperature"] = temperature
            return LLMResponse(
                content="TP53 is a tumor suppressor.",
                model="glm-test",
                finish_reason="stop",
                prompt_tokens=3,
                completion_tokens=7,
                total_tokens=10,
            )

    monkeypatch.setattr("app.llm.glm.GLMClient", FakeClient)
    exit_code = main_module.main(["llm", "--prompt", "hi", "--temperature", "0.2"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Provider: GLM" in output
    assert "Model: glm-test" in output
    assert "TP53 is a tumor suppressor." in output
    assert "total tokens=10" in output
    # default system prompt is prepended, temperature forwarded
    assert seen["messages"][0]["role"] == "system"
    assert seen["messages"][-1] == {"role": "user", "content": "hi"}
    assert seen["temperature"] == 0.2


def test_cli_llm_config_error_exit_code(monkeypatch, capsys):
    class BrokenClient:
        def __init__(self, model=None):
            raise LLMConfigurationError("GLM_API_KEY is not configured")

    monkeypatch.setattr("app.llm.glm.GLMClient", BrokenClient)
    exit_code = main_module.main(["llm", "--prompt", "hi"])
    assert exit_code == main_module.EXIT_TOOL_FAILED
    assert "[llm config error]" in capsys.readouterr().err


def test_cli_llm_invalid_temperature_exit_code(sdk, capsys):
    # real GLMClient with faked SDK: temperature validation fires -> exit 2
    exit_code = main_module.main(["llm", "--prompt", "hi", "--temperature", "5"])
    assert exit_code == main_module.EXIT_BAD_INPUT
    assert "temperature" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Real-network smoke test (opt-in)
# ---------------------------------------------------------------------------

# Allow credentials from the project .env (not just process env) before the
# skip condition is evaluated.
_load_env_for_smoke()
HAS_GLM_CREDS = bool(
    os.environ.get("GLM_API_KEY") and os.environ.get("GLM_MODEL")
)


@pytest.mark.llm_network
@pytest.mark.skipif(
    not HAS_GLM_CREDS, reason="GLM_API_KEY / GLM_MODEL not configured"
)
def test_real_glm_smoke():
    """Hits the live ZhipuAI API. Run with: pytest -m llm_network"""
    client = GLMClient()
    response = client.chat([{"role": "user", "content": "Reply with exactly: OK"}])
    # Output is probabilistic: only assert a non-empty answer, not exact text.
    assert response.content and response.content.strip()
    assert response.model
