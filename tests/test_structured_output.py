"""Offline tests for the Phase 3 structured-output layer.

The LLM is always faked (``FakeLLMClient`` returns canned content strings;
for GLMClient-level tests the SDK is monkeypatched as in Phase 2). One
opt-in real-network test sits at the bottom (marker ``llm_network``).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

import app.llm.glm as glm_module
import main as main_module
from app.config import load_env_file as _load_env_for_smoke
from app.llm.structured import (
    StructuredOutputError,
    chat_structured,
    extract_search_intent,
)
from app.models.schemas import LLMResponse
from app.models.structured import MAX_TOPICS, SearchIntent
from app.prompts.search_intent import SEARCH_INTENT_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

GOOD_INTENT_JSON = json.dumps(
    {"gene": "TP53", "disease": "breast cancer",
     "topics": ["DNA damage", "apoptosis"]}
)


class FakeLLMClient:
    """Minimal structural LLMClient returning a canned content string."""

    def __init__(self, content: str):
        self.content = content
        self.calls: list[dict] = []

    def chat(self, messages, *, model=None, temperature=None, json_mode=False):
        self.calls.append(
            {
                "messages": messages,
                "model": model,
                "temperature": temperature,
                "json_mode": json_mode,
            }
        )
        return LLMResponse(
            content=self.content,
            model="glm-test",
            finish_reason="stop",
            prompt_tokens=11,
            completion_tokens=7,
            total_tokens=18,
        )


# ---------------------------------------------------------------------------
# SearchIntent model (tests 1-10, 13-16 model-level)
# ---------------------------------------------------------------------------

def test_search_intent_normal():
    intent = SearchIntent.model_validate(json.loads(GOOD_INTENT_JSON))
    assert intent.gene == "TP53"
    assert intent.disease == "breast cancer"
    assert intent.topics == ["DNA damage", "apoptosis"]


def test_gene_is_stripped_and_case_preserved():
    assert SearchIntent.model_validate({"gene": "  Trp53 "}).gene == "Trp53"


@pytest.mark.parametrize("bad_gene", ["", "   "])
def test_gene_empty_rejected(bad_gene):
    with pytest.raises(ValidationError, match="gene"):
        SearchIntent.model_validate({"gene": bad_gene})


def test_disease_null_stays_none():
    assert SearchIntent.model_validate({"gene": "TP53", "disease": None}).disease is None
    assert SearchIntent.model_validate({"gene": "TP53"}).disease is None


def test_disease_blank_normalized_to_none():
    assert SearchIntent.model_validate({"gene": "TP53", "disease": "  "}).disease is None
    assert SearchIntent.model_validate(
        {"gene": "TP53", "disease": " breast cancer "}).disease == "breast cancer"


def test_topics_empty_list():
    assert SearchIntent.model_validate({"gene": "TP53"}).topics == []


def test_topics_stripped():
    intent = SearchIntent.model_validate(
        {"gene": "TP53", "topics": [" DNA damage ", "apoptosis"]}
    )
    assert intent.topics == ["DNA damage", "apoptosis"]


def test_topics_empty_strings_dropped():
    intent = SearchIntent.model_validate(
        {"gene": "TP53", "topics": ["a", "", "   ", "b"]}
    )
    assert intent.topics == ["a", "b"]


def test_topics_deduplicated_first_occurrence_order():
    intent = SearchIntent.model_validate(
        {"gene": "TP53", "topics": ["a", "b", "a", " b ", "c"]}
    )
    assert intent.topics == ["a", "b", "c"]


def test_topics_over_limit_rejected_after_cleaning():
    with pytest.raises(ValidationError, match="at most"):
        SearchIntent.model_validate(
            {"gene": "TP53", "topics": [f"topic{i}" for i in range(MAX_TOPICS + 1)]}
        )


def test_topics_under_limit_after_dedup_accepted():
    # 11 raw entries that clean down to 2 unique are fine: the cap applies
    # to the cleaned list, not the raw input.
    intent = SearchIntent.model_validate(
        {"gene": "TP53", "topics": ["a"] * 10 + [" b "]}
    )
    assert intent.topics == ["a", "b"]


def test_wrong_gene_type_rejected():
    with pytest.raises(ValidationError):
        SearchIntent.model_validate({"gene": 123})


@pytest.mark.parametrize(
    "bad_topics",
    ["apoptosis", 42, {"a": 1}, None, [["nested"]], [{"topic": "nested"}]],
)
def test_wrong_topics_type_rejected(bad_topics):
    with pytest.raises(ValidationError):
        SearchIntent.model_validate({"gene": "TP53", "topics": bad_topics})


def test_extra_fields_rejected():
    with pytest.raises(ValidationError, match="extra"):
        SearchIntent.model_validate(
            {"gene": "TP53", "disease": None, "topics": [], "pathway": "p53 signaling"}
        )


# ---------------------------------------------------------------------------
# chat_structured / extract_search_intent (tests 1, 11, 12, 17, 18, 23)
# ---------------------------------------------------------------------------

def test_chat_structured_normal_json():
    result = chat_structured(FakeLLMClient(GOOD_INTENT_JSON), [], SearchIntent)
    assert isinstance(result.data, SearchIntent)
    assert result.data.gene == "TP53"
    # metadata preserved through the wrapper (test 23)
    assert result.raw_response.model == "glm-test"
    assert result.raw_response.total_tokens == 18
    assert result.raw_response.finish_reason == "stop"


def test_chat_structured_malformed_json():
    with pytest.raises(StructuredOutputError, match="not valid JSON"):
        chat_structured(FakeLLMClient("{gene: TP53}"), [], SearchIntent)


def test_chat_structured_natural_language_rejected():
    with pytest.raises(StructuredOutputError, match="not valid JSON"):
        chat_structured(
            FakeLLMClient("Sure! The gene you asked about is TP53."), [], SearchIntent
        )


def test_chat_structured_json_array_rejected_by_schema():
    with pytest.raises(StructuredOutputError, match="schema validation failed"):
        chat_structured(FakeLLMClient("[1, 2, 3]"), [], SearchIntent)


def test_chat_structured_wrong_field_name_rejected_by_schema():
    with pytest.raises(StructuredOutputError, match="schema validation failed"):
        chat_structured(FakeLLMClient('{"genes": "TP53"}'), [], SearchIntent)


def test_chat_structured_error_message_names_offending_fields():
    with pytest.raises(StructuredOutputError, match="gene"):
        chat_structured(FakeLLMClient('{"disease": "breast cancer"}'), [], SearchIntent)


def test_chat_structured_json_mode_forwarded_by_default():
    client = FakeLLMClient(GOOD_INTENT_JSON)
    chat_structured(client, [], SearchIntent)
    assert client.calls[0]["json_mode"] is True
    chat_structured(client, [], SearchIntent, json_mode=False)
    assert client.calls[1]["json_mode"] is False


def test_extract_search_intent_builds_prompt_messages():
    client = FakeLLMClient(GOOD_INTENT_JSON)
    result = extract_search_intent(client, "Study TP53 in breast cancer.")
    messages = client.calls[0]["messages"]
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == SEARCH_INTENT_SYSTEM_PROMPT
    assert messages[1] == {"role": "user", "content": "Study TP53 in breast cancer."}
    assert client.calls[0]["temperature"] == 0.1  # documented extraction default
    assert result.data.disease == "breast cancer"


def test_extract_search_intent_rejects_empty_text():
    with pytest.raises(ValueError, match="non-empty"):
        extract_search_intent(FakeLLMClient(GOOD_INTENT_JSON), "   ")


# ---------------------------------------------------------------------------
# GLMClient json_mode wiring (tests 19-20)
# ---------------------------------------------------------------------------

@pytest.fixture
def sdk(monkeypatch):
    """Fake ZhipuAI SDK + isolated environment (same pattern as Phase 2)."""
    from types import SimpleNamespace

    state = SimpleNamespace(response=None, error=None, instances=[])

    def fake_factory(**init_kwargs):
        instance = SimpleNamespace(init_kwargs=init_kwargs, requests=[])

        def fake_create(**request):
            instance.requests.append(request)
            if state.error is not None:
                raise state.error
            message = SimpleNamespace(content=GOOD_INTENT_JSON)
            choice = SimpleNamespace(message=message, finish_reason="stop")
            return SimpleNamespace(
                choices=[choice],
                model="glm-test-model",
                usage=SimpleNamespace(
                    prompt_tokens=11, completion_tokens=7, total_tokens=18
                ),
            )

        instance.chat = SimpleNamespace(
            completions=SimpleNamespace(create=fake_create)
        )
        state.instances.append(instance)
        return instance

    monkeypatch.setattr(glm_module, "ZhipuAI", fake_factory)
    monkeypatch.setattr(glm_module, "load_env_file", lambda: None)
    monkeypatch.setenv("GLM_API_KEY", "test-key")
    monkeypatch.setenv("GLM_MODEL", "glm-test")
    return state


def test_glm_client_json_mode_false_omits_response_format(sdk):
    glm_module.GLMClient().chat([{"role": "user", "content": "hi"}])
    assert "response_format" not in sdk.instances[0].requests[0]


def test_glm_client_json_mode_true_sends_json_object(sdk):
    glm_module.GLMClient().chat(
        [{"role": "user", "content": "hi"}], json_mode=True
    )
    assert (
        sdk.instances[0].requests[0]["response_format"] == {"type": "json_object"}
    )


def test_glm_client_json_mode_unsupported_sends_nothing(sdk, monkeypatch):
    monkeypatch.setattr(glm_module, "GLM_JSON_MODE_SUPPORTED", False)
    glm_module.GLMClient().chat(
        [{"role": "user", "content": "hi"}], json_mode=True
    )
    assert "response_format" not in sdk.instances[0].requests[0]


def test_extract_via_real_glm_client_and_fake_sdk(sdk):
    client = glm_module.GLMClient()
    result = extract_search_intent(client, "Study TP53 in breast cancer.")
    assert result.data.gene == "TP53"
    request = sdk.instances[0].requests[0]
    assert request["response_format"] == {"type": "json_object"}
    assert request["messages"][0]["role"] == "system"


# ---------------------------------------------------------------------------
# CLI (tests 21-22)
# ---------------------------------------------------------------------------

def _patch_glm_client(monkeypatch, content: str):
    class FakeGLMClient:
        def __init__(self, model=None):
            pass

        def chat(self, messages, *, model=None, temperature=None, json_mode=False):
            return LLMResponse(
                content=content,
                model="glm-test",
                finish_reason="stop",
                prompt_tokens=11,
                completion_tokens=7,
                total_tokens=18,
            )

    monkeypatch.setattr("app.llm.glm.GLMClient", FakeGLMClient)


def test_cli_structured_happy_path(monkeypatch, capsys, tmp_path):
    _patch_glm_client(monkeypatch, GOOD_INTENT_JSON)
    output = tmp_path / "search_intent.json"

    exit_code = main_module.main(
        ["structured", "--prompt", "I want to investigate TP53 in breast cancer.",
         "--output", str(output)]
    )

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "Provider: GLM" in out
    assert "Model: glm-test" in out
    assert "Structured result:" in out
    assert "gene: TP53" in out
    assert "disease: breast cancer" in out
    assert "- DNA damage" in out

    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["gene"] == "TP53"
    assert saved["disease"] == "breast cancer"
    assert saved["topics"] == ["DNA damage", "apoptosis"]
    assert saved["model"] == "glm-test"
    assert saved["usage"]["total_tokens"] == 18
    assert "reasoning" not in " ".join(saved.keys()).lower()


def test_cli_structured_failure_path(monkeypatch, capsys, tmp_path):
    _patch_glm_client(monkeypatch, "Sorry, I cannot answer in JSON right now.")
    exit_code = main_module.main(
        ["structured", "--prompt", "hello", "--output", str(tmp_path / "x.json")]
    )
    assert exit_code == main_module.EXIT_TOOL_FAILED
    assert "[structured error]" in capsys.readouterr().err
    assert not (tmp_path / "x.json").exists()


def test_cli_structured_empty_topics_rendered(monkeypatch, capsys, tmp_path):
    _patch_glm_client(monkeypatch, json.dumps({"gene": "MYC"}))
    output = tmp_path / "intent.json"
    exit_code = main_module.main(
        ["structured", "--prompt", "find MYC papers", "--output", str(output)]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "disease: null" in out
    assert "topics: []" in out


# ---------------------------------------------------------------------------
# Real-network test (opt-in)
# ---------------------------------------------------------------------------

_load_env_for_smoke()
HAS_GLM_CREDS = bool(
    os.environ.get("GLM_API_KEY") and os.environ.get("GLM_MODEL")
)


@pytest.mark.llm_network
@pytest.mark.skipif(
    not HAS_GLM_CREDS, reason="GLM_API_KEY / GLM_MODEL not configured"
)
def test_real_structured_extraction():
    """Live extraction via GLM. Run with: pytest -m llm_network"""
    from app.llm.glm import GLMClient

    result = extract_search_intent(
        GLMClient(), "Extract from this text: Study TP53 in breast cancer focusing on apoptosis."
    )
    assert result.data.gene == "TP53"
    assert result.data.disease and result.data.disease.strip()
    assert any("apoptosis" in topic.lower() for topic in result.data.topics)
