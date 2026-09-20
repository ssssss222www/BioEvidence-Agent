# Phase 4 — Agent Loop: Execution Record

## Goal

The project's first real agent: the LLM can decide to call `search_pubmed`,
the call is validated and executed in Python, the result returns to the
model, and the model produces a final cited answer — under a hard step
budget, behind an allowlist registry, with every failure class
distinguishable.

## 1. LLM vs Agent (revisited, now with code)

Phase 2 gave us an LLM: *input → output, one call, no side effects*.
Phase 4 gives us an **Agent** = LLM + Tools + State + Loop:

```text
LLM:     messages → text.                        (Phase 2)
Agent:   messages → tool_call → result → messages → … → answer   (Phase 4)
```

The model never executes anything; it *requests*. Our Python loop decides
whether the request is legal, runs it, and feeds the result back.

## 2. What Function/Tool Calling is

Tool calling is a structured protocol on top of chat: we show the model a
tool catalogue (name + description + JSON-schema parameters); when the
model wants a tool it returns a machine-readable request instead of (or
besides) text — tool name plus JSON arguments. The API does **not** run the
tool; it only formats the request. Running it is entirely our job.

## 3. What a Tool schema is

`PUBMED_TOOL_DEFINITION` (app/agent/tools.py) is an OpenAI-style
declaration:

```json
{"type": "function", "function": {"name": "search_pubmed",
 "description": "Search PubMed for biomedical literature …",
 "parameters": { …JSON schema… }}}
```

It tells the model *that the tool exists and what its arguments mean*. It
is data, not code — the schema cannot execute anything. Its `parameters`
block is generated from `PubMedSearchArgs.model_json_schema()`, so there is
exactly one source of truth (Pydantic emits `additionalProperties: false`
for `extra="forbid"` models automatically).

## 4. Why the LLM does not directly execute Python

Model output is untrusted input. If it could execute code, a hallucinated
or malicious "argument" would be arbitrary code execution. Instead the
model can only *name* a registered function and pass a JSON object; our
loop parses, validates, and dispatches. The model's power is bounded to
exactly the functions we registered.

## 5. What the Tool registry is for

`TOOL_REGISTRY = {"search_pubmed": execute_pubmed_search}` is an
**allowlist**: the only bridge from model requests to Python. Unknown names
raise `ToolExecutionError("unknown tool …")`. No `eval`, no `globals()`, no
dynamic imports, no model-selected code paths. This is the agent's security
boundary.

## 6. Why tool arguments must be validated twice

The model emits a raw JSON *string*. That string is external, untrusted
input, exactly like Phase 3's structured output:

```text
arguments (str) → json.loads → PubMedSearchArgs.model_validate → search_pubmed
                   layer 1        layer 2
```

Without layer 1, `"{query: TP53}"` would crash with an opaque TypeError.
Without layer 2, `{"query": "TP53", "max_results": 9999}` would bypass the
agent's cost policy (max 5) straight into the API. Both raise
`ToolExecutionError` with distinguishable messages; nothing is repaired or
guessed (fail fast, observable behaviour).

## 7. What Agent state is

In Phase 4, state = the `messages` history, nothing else. Each loop
iteration appends: the assistant's tool-call turn, then one `role="tool"`
result per call. Nothing is persisted between runs — every `agent` CLI
invocation starts fresh.

## 8. What the Agent loop is

`run_literature_agent` (app/agent/loop.py):

```text
messages = [system, user]
for step in 1..MAX_AGENT_STEPS:
    response = client.chat(messages, tools=[pubmed], tool_choice="auto")
    if response.tool_calls:
        append assistant tool-call turn
        for each tool_call: validate → execute → append tool result
        continue
    if response.content:
        return AgentResult(answer, steps, tool_call_count, used_pmids, …)
raise AgentLoopError
```

One iteration = one LLM request. The first text answer ends the run.

## 9. Why MAX_AGENT_STEPS must exist

Without a cap, a model that keeps requesting tools (or a bug in our
message bookkeeping) loops forever, burning tokens and API quota — a
self-inflicted DoS. `MAX_AGENT_STEPS = 4` bounds worst-case cost/latency by
construction; exceeding it raises `AgentLoopError`. There is no
`while True` anywhere in the codebase.

## 10. Why tool_call_id matters

Providers require each `role="tool"` message to reference the exact
assistant tool call it answers (`tool_call_id` ↔ `tool_calls[i].id`).
Break the pairing and the API rejects the follow-up request. The loop
copies `tool_call.id` verbatim into both messages, so pairing is 1:1 by
construction; tests assert it (`test_tool_call_id_pairing_and_message_state`).

## 11. How multiple tool calls are handled

Strategy **A** (sequential execution of every call in the turn): each call
is validated and executed in order, every result is appended as its own
`tool` message, and only after all results are in the history does the next
LLM request happen. Nothing is silently dropped (`test_multiple_tool_calls_
all_executed_sequentially`).

## 12. Phase 4 security boundaries

- Allowlist registry only — the model can reach exactly one function.
- Arguments re-validated (`json.loads` + Pydantic) before execution.
- `max_results` capped at 5 by agent policy (schema `ge=1 le=5`); the
  underlying `search_pubmed` keeps its general capability.
- Tool results are a projection: pmid/title/abstract/journal/year only —
  no authors bulk, no DOI, no HTTP/debug internals fed back to the model.
- Abstracts over 1500 chars are truncated **with an explicit appended
  marker** — never silently.
- API key never appears in messages, results, errors (redaction in
  GLMClient; end-to-end CLI test asserts it).
- Step budget caps total work; unknown tools are refused, not guessed.

## 13. Current limitations

- One tool only (`search_pubmed`); no gene-file tool, no Reactome/NCBI
  Gene (Phase 5), no ranking (Phase 6), no citation verification (Phase 7),
  no HTML report (Phase 8).
- Fail-fast policy: a malformed tool call or a PubMed network error aborts
  the run (no retry/repair by design in Phase 4).
- `tool_choice` is `"auto"`: the model may answer without searching (the
  system prompt pushes against it; the live test's prompt requests a
  search). Forcing a specific function is NOT reliably supported by the
  tested API (see GLM probe below), so no stability was faked.
- Agent state is in-memory only; no conversation persistence.
- glm-5.3 (the configured GLM_MODEL) currently has no account balance;
  live verification used the free-tier glm-4-flash.

## 14. Full request timeline (real run)

```text
User: "Find recent PubMed evidence about the role of TP53 in breast cancer."
  ↓
Agent Loop step 1  → GLMClient.chat(tools=[search_pubmed], tool_choice=auto)
  ↓                ← tool_calls: [{id: call_…, search_pubmed,
                        arguments: '{"query": "...", "max_results": 5}'}]
  ↓ validate (json.loads → PubMedSearchArgs) → registry → search_pubmed
  ↓ PubMed: ESearch → PMIDs → EFetch → Article[]
  ↓ serialize (≤5 articles, abstracts bounded) → role="tool" message
Agent Loop step 2  → GLMClient.chat(messages incl. tool result)
  ↓                ← content: final answer citing real PMIDs
AgentResult(answer, steps=2, tool_call_count=1,
            used_pmids=[37161532, 36739824, 39393354, 38569880, 27815305], …)
```

## ASCII architecture

```text
User
  ↓
Agent Loop (app/agent/loop.py, MAX_AGENT_STEPS=4)
  ↓ messages + tools
LLMClient (protocol) → GLMClient (app/llm/glm.py)
  ↓                          ↑ neutral ToolCall / provider conversion
GLM API (ZhipuAI SDK)
  ↓ tool_call (name + JSON-arguments string)
Tool Registry (app/agent/tools.py, allowlist)
  ↓
Pydantic Validation (PubMedSearchArgs)
  ↓
PubMed (search_pubmed: ESearch → EFetch)
  ↓ Article[]
Tool Result (bounded JSON projection)
  ↓ role="tool" message (tool_call_id paired)
LLM (next step)
  ↓
Final Answer (AgentResult)
```

## GLM tool-calling probe (verified live, 2026-09-19)

| item | result |
|---|---|
| SDK | zhipuai v2.1.5.20250725 (`create()` accepts `tools`, `tool_choice`) |
| model tested | glm-4-flash (glm-5.3 out of balance; free tier used) |
| `tools` supported | yes |
| `tool_choice="auto"` supported | yes — returned `finish_reason="tool_calls"`, `content=None` |
| `tool_calls` structure | `[{id: "call_-7244…", type: "function", function: {name, arguments: "<json str>"}}]` |
| forced `tool_choice={"type":"function",…}` | accepted by the API but did **not** produce a tool call → not used; recorded as a limitation |

Probe code was throwaway (run ad hoc), not part of the codebase.

## Commands executed

```bash
# baseline
D:/anaconda3/python.exe -m pytest                      # 149 passed, 3 deselected
git status                                              # clean

# probe (ad hoc script, not committed)
# → results in the table above

# offline during development
D:/anaconda3/python.exe -m pytest                       # run 1: 183+3 failed → fixed
                                                        # run 2: 186 passed, 4 deselected

# real integration test
GLM_MODEL=glm-4-flash D:/anaconda3/python.exe -m pytest -m agent_network -v
                                                        # 1 passed (15.16s)

# CLI acceptance (real GLM + real PubMed)
D:/anaconda3/python.exe main.py agent --model glm-4-flash \
  --prompt "Find recent PubMed evidence about the role of TP53 in breast cancer."
                                                        # exit 0, 2 steps, 5 PMIDs
```

## Tests

Offline (deterministic, no network):

```text
→ 186 passed, 4 deselected in 2.35s
   (37 new in tests/test_agent_loop.py + 1 structured guard + glm-role fix)
```

All 27 planned cases covered, including: direct answer; tool-call flow;
`content=None`+tool_calls; malformed/missing/blank/out-of-range arguments;
unknown tool; PubMed error; no results; serialization projection;
≤5 articles; id pairing (assistant ↔ tool message); message-state roles;
second-call visibility; multiple sequential tool calls; step exhaustion
(exactly 4 LLM calls); unusable response → `LLMRequestError`; Phase 2/3
regressions through the CLI; json_mode/tools independence; CLI happy/error
paths; end-to-end secret redaction.

Network (opt-in, marker `agent_network`):

- `test_real_agent_integration` — **PASSED** on glm-4-flash: ≥1 real tool
  call, ≥1 real PMID (5 retrieved), non-empty answer, every PMID cited in
  the answer ⊆ retrieved PMIDs.

## Problems encountered

### Problem 1 — test-file mistakes on first run (3 failures)

```text
problem   recursion in the secret-leak test (patched name called itself),
          and two tests used a fixture attribute name that didn't exist
cause     quick drafting: GLMClient was monkeypatched before saving the
          original reference; make_sdk_response vs make_response typo
fix       save the original class before patching; use sdk.make_response
reason    patch-then-call patterns must capture originals up front
result    186 passed offline
```

### Problem 2 — forced tool_choice not reliable (external)

```text
problem   live probe: forced {"type":"function","function":{"name":…}} is
          accepted by the API but did not return a tool call on glm-4-flash
fix       none — agent uses tool_choice="auto" only; the neutral contract
          deliberately limits tool_choice to "auto"/"none"
reason    faking stability we don't have would hide real behaviour;
          the system prompt + task phrasing make tool use very likely
result    live test stable across runs so far; limitation documented
```

### Problem 3 — glm-5.3 balance still exhausted (external, carried from Phase 3)

```text
problem   configured GLM_MODEL (glm-5.3) returns 429/1113 余额不足
fix       all live verification used GLM_MODEL=glm-4-flash / --model
result   live agent test + CLI acceptance passed on glm-4-flash
```

## Security check summary

Working tree reviewed via `git status` / `git diff` (nothing committed or
pushed in this phase): `.env` untouched and untracked (git-ignored);
`prompt/` remains ignored; no API key or credentials in any diff; no new
dependencies beyond what Phase 3 already declared (pydantic); no hidden
reasoning persisted (tool results and CLI output contain only the
documented projections).
