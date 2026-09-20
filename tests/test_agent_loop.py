"""Offline tests for the Phase 4 agent loop, tool layer, and contract v2.

Everything here is fake: the LLM is a scripted client, PubMed is a
monkeypatched function returning canned Articles. One opt-in real
integration test (GLM + live PubMed) sits at the bottom with the
``agent_network`` marker.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest
import zhipuai

import app.agent.tools as agent_tools
import app.llm.glm as glm_module
import main as main_module
from app.agent import (
    MAX_ABSTRACT_CHARS,
    MAX_ARTICLES_TO_MODEL,
    PUBMED_TOOL_DEFINITION,
    AgentLoopError,
    PubMedSearchArgs,
    ToolExecutionError,
    execute_tool_call,
    run_literature_agent,
)
from app.config import load_env_file as _load_env_for_smoke
from app.llm.base import validate_messages
from app.models.schemas import Article, LLMResponse, ToolCall

TEST_KEY = "test-key-abcdef123456"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

def make_article(pmid="11111111", title="TP53 mutations in breast cancer",
                 abstract="TP53 is frequently mutated in breast cancer.",
                 journal="J Example Biomed", year="2023") -> Article:
    return Article(
        pmid=pmid, title=title, abstract=abstract, journal=journal,
        publication_year=year, doi="10.1000/fake.1", authors=["Li Zhang", "John A Smith"],
        pubmed_url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
    )


def tool_call(call_id="call_1", name="search_pubmed",
              arguments='{"query": "TP53 AND breast cancer", "max_results": 2}'
              ) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=arguments)


def make_response(content=None, tool_calls=None) -> LLMResponse:
    return LLMResponse(
        content=content,
        tool_calls=tool_calls or [],
        model="glm-test",
        finish_reason="stop" if content else "tool_calls",
        prompt_tokens=5,
        completion_tokens=7,
        total_tokens=12,
    )


class ScriptedLLMClient:
    """LLMClient fake returning a scripted response sequence."""

    def __init__(self, responses: list[LLMResponse]):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def chat(self, messages, *, model=None, temperature=None,
             json_mode=False, tools=None, tool_choice=None):
        import copy
        self.calls.append(
            {
                "messages": copy.deepcopy(messages),
                "model": model,
                "temperature": temperature,
                "json_mode": json_mode,
                "tools": tools,
                "tool_choice": tool_choice,
            }
        )
        if not self.responses:
            raise AssertionError("scripted LLM responses exhausted")
        return self.responses.pop(0)


@pytest.fixture
def fake_pubmed(monkeypatch):
    """Replace the real PubMed call in the agent tool layer."""
    state = SimpleNamespace(calls=[], articles=[], error=None)

    def fake_search(query, max_results):
        state.calls.append({"query": query, "max_results": max_results})
        if state.error is not None:
            raise state.error
        return state.articles

    monkeypatch.setattr(agent_tools, "search_pubmed", fake_search)
    return state


# ---------------------------------------------------------------------------
# 1-3. loop basics
# ---------------------------------------------------------------------------

def test_agent_direct_final_answer(fake_pubmed):
    client = ScriptedLLMClient([make_response(content="No literature search needed.")])
    result = run_literature_agent(client, "What is a gene?")
    assert result.answer == "No literature search needed."
    assert result.steps == 1
    assert result.tool_call_count == 0
    assert result.used_pmids == []
    assert result.model == "glm-test"
    assert result.total_tokens == 12
    assert fake_pubmed.calls == []


def test_agent_one_tool_call_then_answer(fake_pubmed):
    fake_pubmed.articles = [make_article("111"), make_article("222")]
    client = ScriptedLLMClient(
        [
            make_response(tool_calls=[tool_call()]),
            make_response(content="Based on two articles..."),
        ]
    )
    result = run_literature_agent(client, "Find evidence about TP53 in breast cancer.")
    assert result.steps == 2
    assert result.tool_call_count == 1
    assert result.used_pmids == ["111", "222"]
    assert result.answer == "Based on two articles..."
    assert fake_pubmed.calls == [
        {"query": "TP53 AND breast cancer", "max_results": 2}
    ]


def test_content_none_with_tool_calls_is_valid(fake_pubmed):
    """Case B of the contract: content=None + non-empty tool_calls."""
    fake_pubmed.articles = [make_article("111")]
    client = ScriptedLLMClient(
        [
            make_response(content=None, tool_calls=[tool_call()]),  # content=None
            make_response(content="Answer after tool."),
        ]
    )
    result = run_literature_agent(client, "q")
    assert result.tool_call_count == 1
    assert result.answer == "Answer after tool."


def test_agent_prompt_and_tool_request_shape(fake_pubmed):
    client = ScriptedLLMClient([make_response(content="done")])
    run_literature_agent(client, "hello", temperature=0.2, model="glm-4-flash")
    call = client.calls[0]
    assert call["messages"][0]["role"] == "system"
    assert call["messages"][1] == {"role": "user", "content": "hello"}
    assert call["tools"] == [PUBMED_TOOL_DEFINITION]
    assert call["tool_choice"] == "auto"
    assert call["temperature"] == 0.2
    assert call["model"] == "glm-4-flash"


def test_agent_rejects_empty_prompt():
    with pytest.raises(ValueError, match="non-empty"):
        run_literature_agent(ScriptedLLMClient([]), "   ")


# ---------------------------------------------------------------------------
# 4-11. tool execution errors and edge cases
# ---------------------------------------------------------------------------

def _loop_with(call: ToolCall):
    return ScriptedLLMClient(
        [
            make_response(tool_calls=[call]),
            make_response(content="unreached"),
        ]
    )


def test_malformed_json_arguments(fake_pubmed):
    with pytest.raises(ToolExecutionError, match="invalid JSON arguments"):
        run_literature_agent(_loop_with(tool_call(arguments="{not json")), "q")


def test_missing_query_argument(fake_pubmed):
    with pytest.raises(ToolExecutionError, match="schema validation failed"):
        run_literature_agent(_loop_with(tool_call(arguments='{"max_results": 3}')), "q")


def test_blank_query_argument(fake_pubmed):
    with pytest.raises(ToolExecutionError, match="schema validation failed"):
        run_literature_agent(_loop_with(tool_call(arguments='{"query": "   "}')), "q")


@pytest.mark.parametrize("bad", ['{"query": "TP53", "max_results": 0}',
                                 '{"query": "TP53", "max_results": 6}'])
def test_max_results_out_of_range(fake_pubmed, bad):
    with pytest.raises(ToolExecutionError, match="max_results"):
        run_literature_agent(_loop_with(tool_call(arguments=bad)), "q")


def test_unknown_tool_rejected_by_registry(fake_pubmed):
    with pytest.raises(ToolExecutionError, match="unknown tool"):
        run_literature_agent(
            _loop_with(tool_call(name="delete_everything")), "q"
        )


def test_pubmed_execution_error_wrapped(fake_pubmed):
    from app.tools.pubmed import PubMedError

    fake_pubmed.error = PubMedError("HTTP 503 after 3 attempts")
    with pytest.raises(ToolExecutionError, match="PubMed execution failed"):
        run_literature_agent(_loop_with(tool_call()), "q")


def test_pubmed_no_results_still_answers(fake_pubmed):
    fake_pubmed.articles = []
    client = ScriptedLLMClient(
        [make_response(tool_calls=[tool_call()]), make_response(content="No relevant literature was retrieved.")]
    )
    result = run_literature_agent(client, "q")
    assert result.used_pmids == []
    tool_message = client.calls[1]["messages"][3]
    assert json.loads(tool_message["content"])["retrieved_count"] == 0


# ---------------------------------------------------------------------------
# 12-13. result serialization
# ---------------------------------------------------------------------------

def test_tool_result_serialization_fields_only(fake_pubmed):
    fake_pubmed.articles = [make_article("111", abstract=None)]
    client = ScriptedLLMClient(
        [make_response(tool_calls=[tool_call()]), make_response(content="ok")]
    )
    run_literature_agent(client, "q")
    tool_message = client.calls[1]["messages"][3]
    payload = json.loads(tool_message["content"])
    assert set(payload) == {"query", "retrieved_count", "articles"}
    article = payload["articles"][0]
    assert set(article) == {"pmid", "title", "abstract", "journal", "publication_year"}
    assert "authors" not in article and "doi" not in article
    assert article["abstract"] is None  # missing abstract preserved as null


def test_at_most_five_articles_returned_to_model(fake_pubmed):
    fake_pubmed.articles = [make_article(str(100 + i)) for i in range(6)]
    client = ScriptedLLMClient(
        [make_response(tool_calls=[tool_call()]), make_response(content="ok")]
    )
    result = run_literature_agent(client, "q")
    assert MAX_ARTICLES_TO_MODEL == 5
    assert result.used_pmids == [str(100 + i) for i in range(5)]


def test_abstract_truncated_with_explicit_marker(fake_pubmed):
    fake_pubmed.articles = [make_article("111", abstract="x" * (MAX_ABSTRACT_CHARS + 50))]
    client = ScriptedLLMClient(
        [make_response(tool_calls=[tool_call()]), make_response(content="ok")]
    )
    run_literature_agent(client, "q")
    tool_message = client.calls[1]["messages"][3]
    abstract = json.loads(tool_message["content"])["articles"][0]["abstract"]
    assert f"truncated to {MAX_ABSTRACT_CHARS} characters" in abstract
    assert len(abstract) < MAX_ABSTRACT_CHARS + 100


# ---------------------------------------------------------------------------
# 14-18. message state: ids, roles, visibility to the next call
# ---------------------------------------------------------------------------

def test_tool_call_id_pairing_and_message_state(fake_pubmed):
    fake_pubmed.articles = [make_article("111")]
    client = ScriptedLLMClient(
        [make_response(tool_calls=[tool_call(call_id="call_xyz")]),
         make_response(content="final")]
    )
    run_literature_agent(client, "q")
    second_call_messages = client.calls[1]["messages"]

    assistant_msg = second_call_messages[2]
    tool_msg = second_call_messages[3]
    assert assistant_msg["role"] == "assistant"
    assert assistant_msg["tool_calls"][0]["id"] == "call_xyz"
    assert assistant_msg["tool_calls"][0]["name"] == "search_pubmed"
    assert tool_msg["role"] == "tool"
    assert tool_msg["tool_call_id"] == "call_xyz"  # 1:1 with the tool call id
    # the second LLM call sees the tool result content
    assert '"retrieved_count": 1' in tool_msg["content"]
    assert "111" in tool_msg["content"]
    # and the full history: system, user, assistant(tool), tool
    assert [m["role"] for m in second_call_messages] == [
        "system", "user", "assistant", "tool"
    ]


def test_search_args_schema_and_definition():
    args = PubMedSearchArgs.model_validate({"query": " TP53 ", "max_results": 3})
    assert args.query == "TP53"
    assert args.max_results == 3
    assert PubMedSearchArgs.model_validate({"query": "TP53"}).max_results == 5
    # tool definition derives its schema from the same model (one source)
    schema = PUBMED_TOOL_DEFINITION["function"]["parameters"]
    assert schema == PubMedSearchArgs.model_json_schema()
    assert schema["additionalProperties"] is False
    assert PUBMED_TOOL_DEFINITION["function"]["name"] == "search_pubmed"


# ---------------------------------------------------------------------------
# 19. multiple tool calls in one turn
# ---------------------------------------------------------------------------

def test_multiple_tool_calls_all_executed_sequentially(fake_pubmed):
    fake_pubmed.articles = [make_article("111")]
    first = make_response(
        tool_calls=[
            tool_call(call_id="call_a", arguments='{"query": "TP53", "max_results": 1}'),
            tool_call(call_id="call_b", arguments='{"query": "BRCA1", "max_results": 1}'),
        ]
    )
    client = ScriptedLLMClient([first, make_response(content="both done")])
    result = run_literature_agent(client, "q")

    assert result.tool_call_count == 2
    assert [c["query"] for c in fake_pubmed.calls] == ["TP53", "BRCA1"]  # sequential
    second_call_messages = client.calls[1]["messages"]
    tool_messages = [m for m in second_call_messages if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_messages] == ["call_a", "call_b"]
    # both results were appended before the next LLM request
    assert len(client.calls) == 2


# ---------------------------------------------------------------------------
# 20-21. loop bounds and unusable responses
# ---------------------------------------------------------------------------

def test_max_agent_steps_exhaustion(fake_pubmed):
    fake_pubmed.articles = [make_article("111")]
    looping = ScriptedLLMClient(
        [make_response(tool_calls=[tool_call(call_id=f"c{i}")]) for i in range(10)]
    )
    with pytest.raises(AgentLoopError, match="MAX_AGENT_STEPS"):
        run_literature_agent(looping, "q")
    assert len(looping.calls) == 4  # hard cap, no while True


def test_response_without_content_or_tool_calls_raises(sdk):
    """GLMClient-level guarantee (case 21): unusable bodies fail loudly."""
    from app.llm.base import LLMRequestError

    sdk.response = sdk.make_response(content=None, provider_tool_calls=None)
    client = glm_module.GLMClient()
    with pytest.raises(
        LLMRequestError, match="empty assistant content and no tool_calls"
    ):
        client.chat(
            [{"role": "user", "content": "hi"}], tools=[PUBMED_TOOL_DEFINITION]
        )


# ---------------------------------------------------------------------------
# message contract v2 (validate_messages)
# ---------------------------------------------------------------------------

def test_validate_messages_tool_shapes():
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "name": "search_pubmed", "arguments": "{}"}]},
        {"role": "tool", "tool_call_id": "c1", "content": '{"ok": true}'},
    ]
    validated = validate_messages(messages)
    assert validated[2]["tool_calls"][0]["id"] == "c1"
    assert validated[3]["tool_call_id"] == "c1"


def test_validate_messages_tool_role_requires_tool_call_id():
    with pytest.raises(ValueError, match="tool_call_id"):
        validate_messages([{"role": "tool", "content": "{}"}])


def test_validate_messages_assistant_none_content_needs_tool_calls():
    with pytest.raises(ValueError, match="content or"):
        validate_messages([{"role": "assistant", "content": None}])


def test_validate_messages_tool_call_requires_id_and_name():
    with pytest.raises(ValueError, match="non-empty id"):
        validate_messages([
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "", "name": "x", "arguments": "{}"}]}
        ])
    with pytest.raises(ValueError, match="non-empty name"):
        validate_messages([
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "c", "name": " ", "arguments": "{}"}]}
        ])


# ---------------------------------------------------------------------------
# 24. GLMClient wiring: tools / tool_choice / json_mode independence
# ---------------------------------------------------------------------------

@pytest.fixture
def sdk(monkeypatch):
    state = SimpleNamespace(response=None, error=None, instances=[])

    def make_sdk_response(content="ok", provider_tool_calls=None):
        message = SimpleNamespace(content=content, tool_calls=provider_tool_calls)
        choice = SimpleNamespace(message=message, finish_reason="stop")
        return SimpleNamespace(
            choices=[choice], model="glm-test-model",
            usage=SimpleNamespace(prompt_tokens=5, completion_tokens=7, total_tokens=12),
        )

    state.make_response = make_sdk_response

    def fake_factory(**init_kwargs):
        instance = SimpleNamespace(init_kwargs=init_kwargs, requests=[])

        def fake_create(**request):
            instance.requests.append(request)
            if state.error is not None:
                raise state.error
            return state.response if state.response is not None else make_sdk_response()

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


def test_json_mode_path_unaffected_by_tool_support(sdk):
    client = glm_module.GLMClient()
    client.chat([{"role": "user", "content": "hi"}], json_mode=True)
    request = sdk.instances[0].requests[0]
    assert request["response_format"] == {"type": "json_object"}
    assert "tools" not in request and "tool_choice" not in request


def test_tools_and_tool_choice_sent(sdk):
    client = glm_module.GLMClient()
    client.chat(
        [{"role": "user", "content": "hi"}],
        tools=[PUBMED_TOOL_DEFINITION],
        tool_choice="auto",
    )
    request = sdk.instances[0].requests[0]
    assert request["tools"] == [PUBMED_TOOL_DEFINITION]
    assert request["tool_choice"] == "auto"
    assert "response_format" not in request


def test_invalid_tool_choice_rejected(sdk):
    client = glm_module.GLMClient()
    with pytest.raises(ValueError, match="tool_choice"):
        client.chat(
            [{"role": "user", "content": "hi"}],
            tools=[PUBMED_TOOL_DEFINITION],
            tool_choice="required",
        )


def test_provider_tool_calls_parsed_to_neutral_toolcall(sdk):
    sdk.response = sdk.make_response(
        content=None,
        provider_tool_calls=[
            SimpleNamespace(
                id="call_123",
                type="function",
                function=SimpleNamespace(
                    name="search_pubmed", arguments='{"query": "TP53"}'
                ),
            )
        ],
    )
    response = glm_module.GLMClient().chat(
        [{"role": "user", "content": "hi"}], tools=[PUBMED_TOOL_DEFINITION]
    )
    assert response.content is None
    assert response.tool_calls == [
        ToolCall(id="call_123", name="search_pubmed", arguments='{"query": "TP53"}')
    ]


def test_neutral_messages_converted_to_provider_format(sdk):
    client = glm_module.GLMClient()
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None,
         "tool_calls": [
             {"id": "c1", "name": "search_pubmed", "arguments": '{"query": "q"}'}
         ]},
        {"role": "tool", "tool_call_id": "c1", "content": '{"retrieved_count": 0}'},
    ]
    client.chat(messages, tools=[PUBMED_TOOL_DEFINITION], tool_choice="auto")
    sent = sdk.instances[0].requests[0]["messages"]
    assert sent[1]["tool_calls"][0] == {
        "id": "c1",
        "type": "function",
        "function": {"name": "search_pubmed", "arguments": '{"query": "q"}'},
    }
    assert sent[2] == {
        "role": "tool", "tool_call_id": "c1", "content": '{"retrieved_count": 0}',
    }


# ---------------------------------------------------------------------------
# 25-27. CLI
# ---------------------------------------------------------------------------

def _patch_agent_client(monkeypatch, client):
    class FakeGLMClient:
        def __init__(self, model=None):
            pass

        def chat(self, *args, **kwargs):
            return client.chat(*args, **kwargs)

    monkeypatch.setattr("app.llm.glm.GLMClient", FakeGLMClient)


def test_cli_agent_happy_path(monkeypatch, capsys, fake_pubmed):
    fake_pubmed.articles = [make_article("111"), make_article("222")]
    _patch_agent_client(
        monkeypatch,
        ScriptedLLMClient(
            [make_response(tool_calls=[tool_call()]),
             make_response(content="TP53 is a tumor suppressor (PMID 111).")]
        ),
    )
    exit_code = main_module.main(
        ["agent", "--prompt", "Find evidence about TP53 in breast cancer."]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "Provider: GLM" in out
    assert "Model: glm-test" in out
    assert "Steps: 2" in out
    assert "Tool calls: 1" in out
    assert "111" in out and "222" in out
    assert "Answer:" in out
    assert "PMID 111" in out


def test_cli_agent_tool_error_path(monkeypatch, capsys, fake_pubmed):
    _patch_agent_client(
        monkeypatch,
        ScriptedLLMClient([make_response(tool_calls=[tool_call(name="nope")])]),
    )
    exit_code = main_module.main(["agent", "--prompt", "q"])
    assert exit_code == main_module.EXIT_TOOL_FAILED
    err = capsys.readouterr().err
    assert "[tool error]" in err and "unknown tool" in err


def test_cli_agent_loop_error_path(monkeypatch, capsys, fake_pubmed):
    fake_pubmed.articles = [make_article("111")]
    _patch_agent_client(
        monkeypatch,
        ScriptedLLMClient(
            [make_response(tool_calls=[tool_call(call_id=f"c{i}")]) for i in range(10)]
        ),
    )
    exit_code = main_module.main(["agent", "--prompt", "q"])
    assert exit_code == main_module.EXIT_TOOL_FAILED
    assert "[agent error]" in capsys.readouterr().err


def test_cli_agent_no_secret_leak(monkeypatch, capsys, sdk):
    """End-to-end secret safety: SDK error embeds the key -> the CLI must
    print a redacted message (redaction happens in GLMClient)."""
    sdk.error = zhipuai.ZhipuAIError(f"boom with key {TEST_KEY}")

    real_glm_client = glm_module.GLMClient  # save before patching (no recursion)

    class RealGLMPath:  # real GLMClient logic over the fake SDK
        def __init__(self, model=None):
            self._inner = real_glm_client(model=model)

        def chat(self, *args, **kwargs):
            return self._inner.chat(*args, **kwargs)

    monkeypatch.setattr("app.llm.glm.GLMClient", RealGLMPath)
    exit_code = main_module.main(["agent", "--prompt", "q"])
    assert exit_code == main_module.EXIT_TOOL_FAILED
    err = capsys.readouterr().err
    assert TEST_KEY not in err
    assert "***REDACTED***" in err


# ---------------------------------------------------------------------------
# 22-23. explicit regressions through the CLI (Phase 2/3 paths)
# ---------------------------------------------------------------------------

def test_phase2_llm_cli_still_works(monkeypatch, capsys):
    class FakeGLMClient:
        def __init__(self, model=None):
            pass

        def chat(self, messages, *, model=None, temperature=None, json_mode=False):
            return LLMResponse(content="plain answer", model="glm-test",
                               finish_reason="stop", total_tokens=3)

    monkeypatch.setattr("app.llm.glm.GLMClient", FakeGLMClient)
    exit_code = main_module.main(["llm", "--prompt", "hi"])
    assert exit_code == 0
    assert "plain answer" in capsys.readouterr().out


def test_phase3_structured_cli_still_works(monkeypatch, capsys, tmp_path):
    class FakeGLMClient:
        def __init__(self, model=None):
            pass

        def chat(self, messages, *, model=None, temperature=None, json_mode=False):
            return LLMResponse(
                content='{"gene": "TP53"}', model="glm-test",
                finish_reason="stop", total_tokens=3,
            )

    monkeypatch.setattr("app.llm.glm.GLMClient", FakeGLMClient)
    exit_code = main_module.main(
        ["structured", "--prompt", "x", "--output", str(tmp_path / "i.json")]
    )
    assert exit_code == 0
    assert "gene: TP53" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Real integration test (opt-in)
# ---------------------------------------------------------------------------

_load_env_for_smoke()
HAS_GLM_CREDS = bool(
    os.environ.get("GLM_API_KEY") and os.environ.get("GLM_MODEL")
)


@pytest.mark.agent_network
@pytest.mark.skipif(
    not HAS_GLM_CREDS, reason="GLM_API_KEY / GLM_MODEL not configured"
)
def test_real_agent_integration():
    """Live GLM + live PubMed. Run with: pytest -m agent_network"""
    import re

    from app.llm.glm import GLMClient

    class RecordingClient:
        """Pass-through LLMClient that records the tool calls the model
        actually requested, so the test can verify real arguments."""

        def __init__(self, inner):
            self._inner = inner
            self.requested_tool_calls = []

        def chat(self, messages, **kwargs):
            response = self._inner.chat(messages, **kwargs)
            self.requested_tool_calls.extend(response.tool_calls)
            return response

    recording = RecordingClient(GLMClient())
    result = run_literature_agent(
        recording,
        "Find recent PubMed evidence about the role of TP53 in breast "
        "cancer. Use PubMed and retrieve at most 2 results (set "
        "max_results=2). Cite every article you mention in the form "
        '"PMID: <pmid>".',
    )
    assert result.tool_call_count >= 1, "agent should use the PubMed tool"
    assert recording.requested_tool_calls, "expected captured tool calls"
    # Verify the REAL tool arguments, not just the prompt's request.
    for call in recording.requested_tool_calls:
        parsed_args = PubMedSearchArgs.model_validate(json.loads(call.arguments))
        assert parsed_args.max_results <= 2, (
            f"model requested max_results={parsed_args.max_results}; the "
            "test prompt must keep the live call light"
        )
    assert result.used_pmids, "expected at least one retrieved article"
    assert len(result.used_pmids) <= 2  # returned articles honour the cap
    assert all(pmid.isdigit() for pmid in result.used_pmids)
    assert result.answer.strip()
    # Only explicit citations count: "PMID: 12345678" (case-insensitive).
    # Bare numbers (years, counts, gene names) must never be read as PMIDs.
    cited = set(re.findall(r"PMID\s*:?\s*(\d+)", result.answer, re.IGNORECASE))
    assert cited, "answer must contain at least one explicit PMID citation"
    assert cited <= set(result.used_pmids), (
        f"answer cited unknown PMIDs {cited - set(result.used_pmids)}"
    )
