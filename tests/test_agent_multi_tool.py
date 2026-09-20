"""Phase 5 multi-tool tests: registry, tool selection/composition through
the agent loop, provenance, and opt-in real-network tests (NCBI Gene,
Reactome, and a multi-tool live agent run). Offline tests fake the LLM
(scripted responses) and the underlying tools (monkeypatched functions).
"""

from __future__ import annotations

import json
import os
import re

import pytest
from pydantic import ValidationError

import app.agent.tools as agent_tools
from app.agent import (
    GET_GENE_INFO_TOOL,
    GET_REACTOME_PATHWAYS_TOOL,
    SEARCH_PUBMED_TOOL,
    TOOL_DEFINITIONS,
    TOOL_REGISTRY,
    AgentLoopError,
    GeneInfoArgs,
    PubMedSearchArgs,
    ReactomePathwayArgs,
    ToolExecutionError,
    execute_tool_call,
    run_literature_agent,
)
from app.config import load_env_file as _load_env_for_smoke
from app.models.schemas import (
    Article,
    GeneInfo,
    LLMResponse,
    ReactomePathway,
    ToolCall,
)

# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

FAKE_GENE = GeneInfo(
    gene_id="7157", symbol="TP53", name="tumor protein p53",
    organism="Homo sapiens", tax_id="9606", chromosome="17",
    map_location="17p13.1", aliases=["P53", "TRP53"],
    summary="Tumor suppressor.",
)
FAKE_PATHWAYS = [
    ReactomePathway(stable_id="R-HSA-6804754", name="Regulation of TP53 Expression",
                    species="Homo sapiens", is_disease=False),
    ReactomePathway(stable_id="R-HSA-9723907", name="Loss of Function of TP53 in Cancer",
                    species="Homo sapiens", is_disease=True),
]
FAKE_ARTICLES = [
    Article(pmid="36739824", title="Germline TP53 pathogenic variants",
            abstract="...", journal="Cancer Treat Rev", publication_year="2023"),
]


@pytest.fixture
def fake_domain_tools(monkeypatch):
    state = {"calls": []}

    def fake_gene(gene, species):
        state["calls"].append(("get_gene_info", gene, species))
        return FAKE_GENE

    def fake_pathways(gene, species, max_results=5):
        state["calls"].append(("get_reactome_pathways", gene, species, max_results))
        return FAKE_PATHWAYS[:max_results]

    def fake_pubmed(query, max_results):
        state["calls"].append(("search_pubmed", query, max_results))
        return FAKE_ARTICLES[:max_results]

    monkeypatch.setattr(agent_tools, "get_gene_info", fake_gene)
    monkeypatch.setattr(agent_tools, "get_reactome_pathways", fake_pathways)
    monkeypatch.setattr(agent_tools, "search_pubmed", fake_pubmed)
    return state


class ScriptedLLMClient:
    def __init__(self, responses):
        import copy
        self._copy = copy
        self.responses = list(responses)
        self.calls = []

    def chat(self, messages, *, model=None, temperature=None,
             json_mode=False, tools=None, tool_choice=None):
        self.calls.append({
            "messages": self._copy.deepcopy(messages),
            "tools": tools, "tool_choice": tool_choice,
        })
        if not self.responses:
            raise AssertionError("scripted responses exhausted")
        return self.responses.pop(0)


def response(content=None, tool_calls=None):
    return LLMResponse(content=content, tool_calls=tool_calls or [],
                       model="glm-test", finish_reason="stop",
                       prompt_tokens=5, completion_tokens=7, total_tokens=12)


def call(call_id, name, arguments):
    return ToolCall(id=call_id, name=name, arguments=json.dumps(arguments))


# ---------------------------------------------------------------------------
# registry & schemas
# ---------------------------------------------------------------------------

def test_registry_has_three_known_tools():
    assert set(TOOL_REGISTRY) == {
        "search_pubmed", "get_gene_info", "get_reactome_pathways"
    }
    assert set(TOOL_DEFINITIONS) == set(TOOL_REGISTRY)


def test_unknown_tool_rejected(fake_domain_tools):
    with pytest.raises(ToolExecutionError, match="unknown tool"):
        execute_tool_call(ToolCall(id="c", name="delete_all", arguments="{}"))


def test_tool_definitions_derive_from_args_models():
    assert SEARCH_PUBMED_TOOL["function"]["parameters"] == \
        PubMedSearchArgs.model_json_schema()
    assert GET_GENE_INFO_TOOL["function"]["parameters"] == \
        GeneInfoArgs.model_json_schema()
    assert GET_REACTOME_PATHWAYS_TOOL["function"]["parameters"] == \
        ReactomePathwayArgs.model_json_schema()
    for definition in TOOL_DEFINITIONS.values():
        assert definition["function"]["parameters"]["additionalProperties"] is False


@pytest.mark.parametrize("model", [GeneInfoArgs, ReactomePathwayArgs])
def test_args_reject_blank_fields(model):
    with pytest.raises(ValidationError):
        model.model_validate({"gene": "  ", "species": "human"})
    with pytest.raises(ValidationError):
        model.model_validate({"gene": "TP53", "species": ""})


def test_reactome_args_max_results_bounds():
    assert ReactomePathwayArgs.model_validate(
        {"gene": "TP53", "species": "human"}).max_results == 5
    with pytest.raises(ValidationError):
        ReactomePathwayArgs.model_validate(
            {"gene": "TP53", "species": "human", "max_results": 11})
    with pytest.raises(ValidationError):
        ReactomePathwayArgs.model_validate(
            {"gene": "TP53", "species": "human", "max_results": 0})


def test_registry_dispatch_correct(fake_domain_tools):
    result = execute_tool_call(call("c1", "get_gene_info",
                                    {"gene": "TP53", "species": "human"}))
    payload = json.loads(result.output)
    assert payload["source"] == "NCBI Gene"
    assert payload["gene"]["gene_id"] == "7157"
    assert result.gene_ids == ["7157"]

    result = execute_tool_call(call("c2", "get_reactome_pathways",
                                    {"gene": "TP53", "species": "human",
                                     "max_results": 1}))
    payload = json.loads(result.output)
    assert payload["source"] == "Reactome"
    assert payload["pathways"][0]["stable_id"] == "R-HSA-6804754"
    assert result.reactome_ids == ["R-HSA-6804754"]
    assert fake_domain_tools["calls"][-1] == \
        ("get_reactome_pathways", "TP53", "human", 1)


def test_gene_summary_truncated_with_marker(fake_domain_tools, monkeypatch):
    monkeypatch.setattr(agent_tools, "get_gene_info",
                        lambda gene, species: GeneInfo(
                            gene_id="1", symbol="X",
                            summary="s" * (agent_tools.MAX_SUMMARY_CHARS + 10)))
    result = execute_tool_call(call("c", "get_gene_info",
                                    {"gene": "X", "species": "human"}))
    summary = json.loads(result.output)["gene"]["summary"]
    assert f"truncated to {agent_tools.MAX_SUMMARY_CHARS} characters" in summary


# ---------------------------------------------------------------------------
# agent tool selection & composition (offline)
# ---------------------------------------------------------------------------

def test_gene_question_uses_gene_tool(fake_domain_tools):
    client = ScriptedLLMClient([
        response(tool_calls=[call("c1", "get_gene_info",
                                  {"gene": "TP53", "species": "human"})]),
        response(content="TP53 (GeneID: 7157) is a tumor suppressor."),
    ])
    result = run_literature_agent(client, "What is human TP53?")
    assert result.used_gene_ids == ["7157"]
    assert result.used_pmids == [] and result.used_reactome_ids == []
    assert fake_domain_tools["calls"] == [("get_gene_info", "TP53", "human")]
    tool_message = client.calls[1]["messages"][3]
    assert json.loads(tool_message["content"])["source"] == "NCBI Gene"


def test_pathway_question_uses_reactome_tool(fake_domain_tools):
    client = ScriptedLLMClient([
        response(tool_calls=[call("c1", "get_reactome_pathways",
                                  {"gene": "TP53", "species": "human",
                                   "max_results": 2})]),
        response(content="TP53 maps to R-HSA-6804754 and R-HSA-9723907."),
    ])
    result = run_literature_agent(client, "Which Reactome pathways involve human TP53?")
    assert result.used_reactome_ids == ["R-HSA-6804754", "R-HSA-9723907"]
    assert fake_domain_tools["calls"][0][0] == "get_reactome_pathways"


def test_literature_question_uses_pubmed(fake_domain_tools):
    client = ScriptedLLMClient([
        response(tool_calls=[call("c1", "search_pubmed",
                                  {"query": "TP53 AND breast cancer"})]),
        response(content="Evidence indicates... (PMID: 36739824)"),
    ])
    result = run_literature_agent(client, "Find evidence linking TP53 to breast cancer.")
    assert result.used_pmids == ["36739824"]
    assert fake_domain_tools["calls"][0][0] == "search_pubmed"


def test_mixed_question_composes_all_three_tools(fake_domain_tools):
    first = response(tool_calls=[
        call("c1", "get_gene_info", {"gene": "TP53", "species": "human"}),
        call("c2", "get_reactome_pathways", {"gene": "TP53", "species": "human"}),
        call("c3", "search_pubmed", {"query": "TP53 AND breast cancer",
                                     "max_results": 2}),
    ])
    client = ScriptedLLMClient([first, response(content="composed answer")])
    result = run_literature_agent(client, "Explain human TP53, its pathways, "
                                          "and breast cancer evidence.")
    assert result.tool_call_count == 3
    assert result.used_gene_ids == ["7157"]
    assert result.used_reactome_ids == ["R-HSA-6804754", "R-HSA-9723907"]
    assert result.used_pmids == ["36739824"]
    # all three results visible on the next LLM call, ids paired 1:1
    second_messages = client.calls[1]["messages"]
    tool_messages = [m for m in second_messages if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_messages] == ["c1", "c2", "c3"]
    sources = [json.loads(m["content"])["source"] for m in tool_messages]
    assert sources == ["NCBI Gene", "Reactome", "PubMed"]
    assert len(client.calls) == 2


def test_all_three_definitions_sent_to_llm(fake_domain_tools):
    client = ScriptedLLMClient([response(content="ok")])
    run_literature_agent(client, "hi")
    assert client.calls[0]["tools"] == \
        [TOOL_DEFINITIONS[name] for name in TOOL_REGISTRY]


def test_loop_still_bounded_with_new_tools(fake_domain_tools):
    looping = ScriptedLLMClient(
        [response(tool_calls=[call(f"c{i}", "get_gene_info",
                                   {"gene": "TP53", "species": "human"})])
         for i in range(10)]
    )
    with pytest.raises(AgentLoopError, match="MAX_AGENT_STEPS"):
        run_literature_agent(looping, "q")
    assert len(looping.calls) == 6  # MAX_AGENT_STEPS (raised to 6 in review)


def test_sequential_multi_tool_sequence_finishes_within_budget(fake_domain_tools):
    """3 tool calls in 3 separate turns + final synthesis = 4 steps — must
    complete well within the MAX_AGENT_STEPS=6 budget."""
    from app.agent.loop import MAX_AGENT_STEPS

    client = ScriptedLLMClient([
        response(tool_calls=[call("c1", "get_gene_info",
                                  {"gene": "TP53", "species": "human"})]),
        response(tool_calls=[call("c2", "get_reactome_pathways",
                                  {"gene": "TP53", "species": "human"})]),
        response(tool_calls=[call("c3", "search_pubmed",
                                  {"query": "TP53 AND breast cancer"})]),
        response(content="synthesis done"),
    ])
    result = run_literature_agent(
        client, "Explain human TP53, its pathways, and breast cancer evidence."
    )
    assert result.answer == "synthesis done"
    assert result.steps == 4
    assert result.steps <= MAX_AGENT_STEPS
    assert result.tool_call_count == 3


# ---------------------------------------------------------------------------
# real-network tests (opt-in)
# ---------------------------------------------------------------------------

_load_env_for_smoke()
HAS_GLM_CREDS = bool(
    os.environ.get("GLM_API_KEY") and os.environ.get("GLM_MODEL")
)


@pytest.mark.ncbi_network
def test_real_ncbi_gene_tp53():
    from app.tools.ncbi_gene import get_gene_info

    info = get_gene_info("TP53", "human")
    assert info.symbol == "TP53"
    assert info.gene_id and info.gene_id.isdigit()
    assert info.organism and "Homo sapiens" in info.organism
    assert info.summary or info.name  # at least one meaningful text field


@pytest.mark.ncbi_network
def test_real_ncbi_gene_case_insensitive_official_casing():
    """Lowercase requests must resolve to the official casing (the [sym]
    field is case-insensitive server-side; output casing is NCBI's)."""
    from app.tools.ncbi_gene import get_gene_info

    assert get_gene_info("tp53", "human").symbol == "TP53"
    mouse = get_gene_info("trp53", "Mus musculus")
    assert mouse.symbol == "Trp53"
    assert mouse.organism == "Mus musculus"


@pytest.mark.reactome_network
def test_real_reactome_tp53():
    from app.tools.reactome import get_reactome_pathways

    pathways = get_reactome_pathways("TP53", "Homo sapiens", max_results=3)
    assert pathways, "expected at least one pathway for TP53"
    assert len(pathways) <= 3
    for pathway in pathways:
        assert pathway.stable_id.startswith("R-")
        assert pathway.name
        assert pathway.species == "Homo sapiens"


@pytest.mark.agent_network
@pytest.mark.skipif(
    not HAS_GLM_CREDS, reason="GLM_API_KEY / GLM_MODEL not configured"
)
def test_real_agent_multi_tool():
    """Live GLM driving real NCBI Gene + Reactome (no PubMed here, to keep
    one test off a third remote service). Run with: pytest -m agent_network"""
    from app.llm.glm import GLMClient

    class RecordingClient:
        def __init__(self, inner):
            self._inner = inner
            self.requested = []

        def chat(self, messages, **kwargs):
            response = self._inner.chat(messages, **kwargs)
            self.requested.extend(response.tool_calls)
            return response

    recording = RecordingClient(GLMClient())
    result = run_literature_agent(
        recording,
        "Use both the NCBI Gene and Reactome tools to explain human TP53 "
        "and its pathways. Cite the GeneID as 'GeneID: <id>' and each "
        "pathway as its R-HSA- stable ID.",
    )
    names = [tool_call.name for tool_call in recording.requested]
    assert "get_gene_info" in names
    assert "get_reactome_pathways" in names
    for tool_call in recording.requested:
        model = {
            "get_gene_info": GeneInfoArgs,
            "get_reactome_pathways": ReactomePathwayArgs,
            "search_pubmed": PubMedSearchArgs,
        }[tool_call.name]
        model.model_validate(json.loads(tool_call.arguments))
    assert result.answer.strip()
    assert result.used_gene_ids and result.used_reactome_ids
    # identifiers cited in the answer must come from real tool results
    cited_gene_ids = set(re.findall(r"GeneID:?\s*(\d+)", result.answer,
                                    re.IGNORECASE))
    assert cited_gene_ids <= set(result.used_gene_ids)
    cited_reactome = set(re.findall(r"(R-HSA-\d+)", result.answer))
    assert cited_reactome <= set(result.used_reactome_ids)
