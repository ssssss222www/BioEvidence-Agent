# Phase 2 — GLM Client: Execution Record

## Goal

Build a reliable, provider-decoupled LLM calling layer: messages in, a
normalized `LLMResponse` out, with honest configuration errors, bounded
failures and no secret leakage. This phase adds *no* agent behaviour — the
LLM is a library component that later phases will drive.

## Environment

| component | value |
|---|---|
| OS | Windows (win32), Git Bash |
| Python | 3.12.7 (Anaconda) |
| SDK | `zhipuai` 2.1.5 (official ZhipuAI SDK, installed via pip during this phase) |
| other deps | requests 2.32.3, pandas 2.2.2, openpyxl 3.1.5, pytest 7.4.4 |
| credentials | real `GLM_API_KEY` + `GLM_MODEL` present in local `.env` (never printed, never committed) |

## API request — how Python calls GLM

The official SDK exposes an OpenAI-style client:

```python
from zhipuai import ZhipuAI

client = ZhipuAI(api_key=..., timeout=60.0, max_retries=2)
raw = client.chat.completions.create(
    model="glm-4-flash",          # required
    messages=[{"role": "user", "content": "..."}],
    stream=False,                  # Phase 2 is non-streaming only
    temperature=0.2,               # optional; omit to use the API default
)
raw.choices[0].message.content     # the answer text
raw.model, raw.choices[0].finish_reason, raw.usage.prompt_tokens, ...
```

This is the current (non-deprecated) v2 API; the legacy `zhipuai.model_api`
module is not used. Our `GLMClient` is a thin adapter around exactly this
call: it validates messages first, resolves model/temperature, and
normalizes the response into `LLMResponse`. The "智能体平台" (agent
platform) is deliberately not used — GLM is only the underlying LLM here;
any agent loop will be ours, built in a later phase.

## Messages — what system / user / assistant mean

A *message list* is the conversation so far, sent with every request:

- **system** — standing instructions that frame the whole conversation
  ("You are a precise biomedical research assistant."). Highest priority,
  written by the application, not the user.
- **user** — what the human wants in this turn (in our future agent: often
  a generated query or a question about retrieved evidence).
- **assistant** — what the model previously answered. Resending it gives
  the model its own history so a follow-up makes sense (multi-turn context).

Phase 2 supports exactly these three roles (validated by
`validate_messages`); a `tool` role is intentionally rejected until the
tool-calling phase. Messages are `list[dict[str, str]]` — the wire format
every OpenAI-style SDK already accepts — rather than a custom dataclass:
fewer layers, same guarantees.

## LLM vs Agent — what we have and what we do not

We now **have an LLM** in the project. We still **do not have an Agent**.

```text
LLM:    input (messages) → output (text). One call, no side effects,
        no memory, no decisions about what to do next.

Agent:  LLM + Tools + State + Loop. The model chooses which tool to call
        (e.g. search_pubmed), observes results, decides the next step,
        and stops when the goal is met.
```

Phase 2 provides the "LLM" box only. The tools (`search_pubmed`,
`parse_gene_file`) exist but nothing connects them to the model yet — that
is Phase 4 (tool calling / agent loop), built on Phase 3 (structured
output).

## Provider abstraction — why not depend on ZhipuAI everywhere

If every caller touched the SDK directly, `ZhipuAIError`,
`choices[0].message.content` and SDK version churn would spread through the
codebase, and switching or adding providers would mean editing every call
site. Instead:

```text
Application / Agent (future)
    ↓
LLMClient protocol (app/llm/base.py) + LLMResponse (schemas.py)
    ↓
GLMClient (app/llm/glm.py)   ← only file that imports zhipuai
    ↓
GLM API (ZhipuAI SDK, chat.completions)
```

Later, additional providers slot in without touching callers:

```text
LLMClient
├── GLMClient      (implemented — Phase 2)
├── QwenClient     (future)
└── DeepSeekClient (future)
```

Only `GLMClient` is implemented. The interface is a `typing.Protocol`
(structural typing) rather than an ABC: conforming classes need no
inheritance and no import of a base class, so test fakes and future
providers plug in with zero ceremony.

## LLMResponse — why another data contract

Same reasoning as `Article` (Phase 0) and `GeneRecord` (Phase 1): the rest
of the project must program against one stable, documented shape — never
against `raw.choices[0].message.content` chains or SDK objects whose
attributes vary by provider/model. Fields: `content` (always non-empty),
`model` (API echo with fallback to the requested name), `finish_reason`,
and the three token counters (`None` when the API omits usage). Hidden
`reasoning_content` (thinking models) is deliberately **not** part of the
contract: we neither use nor persist model chain-of-thought.

## Environment variables — `.env` / `.env.example` / `.gitignore`

- **`.env`** — local secrets/config (real `GLM_API_KEY`, `GLM_MODEL`, and
  optional `NCBI_*`). Read once by `app/config.py`; values already present
  in the real environment always win. Must stay out of version control.
- **`.env.example`** — committed template listing every supported variable
  with placeholder values, so new developers know what to configure
  without ever seeing real secrets.
- **`.gitignore`** — contains `.env` so the real file can never be
  committed accidentally; `outputs/*` and caches are ignored too.

Model name resolution: CLI `--model` → `GLM_MODEL` → clear
`LLMConfigurationError`. Nothing is hard-coded in business logic.

## Retry policy (choice + reason)

The official SDK already retries transient HTTP failures internally
(`max_retries` constructor parameter, default 3). `GLMClient` pins it to
`max_retries=2` and adds **no** custom retry loop: stacked retries multiply
worst-case latency and can amplify rate limits. Authentication failures are
configuration problems and are never retried by us.

## Testing — mock unit tests vs real API smoke test

- **Mock unit tests** (34, offline, run under plain `pytest`): the SDK is
  replaced by a fake injected at `app.llm.glm.ZhipuAI`; responses and
  exceptions are `SimpleNamespace`s. These verify contract behaviour —
  validation, request shape (`stream=False`, model/temperature routing),
  response normalization (usage present/absent, model fallback,
  finish_reason), malformed-response errors, SDK-exception mapping
  (auth → `LLMConfigurationError`, others → `LLMRequestError`), API-key
  redaction, and CLI orchestration (exit codes, output format). They never
  touch the network, so they are deterministic, fast and CI-safe.
- **Real API smoke test** (1, marker `llm_network`, excluded by default):
  runs only with `pytest -m llm_network` and only when `GLM_API_KEY` +
  `GLM_MODEL` exist (else skipped). Sends "Reply with exactly: OK",
  asserts a non-empty answer and a model name — deliberately not the exact
  string "OK", because model output is probabilistic. It proves the
  end-to-end path (SDK, auth, request, normalization) works against the
  live service and costs a handful of tokens.

## Commands executed

```bash
# install the official SDK
D:/anaconda3/python.exe -m pip install "zhipuai>=2.0"        # → zhipuai 2.1.5

# offline suite (before adding the smoke-marker config, after implementation)
D:/anaconda3/python.exe -m pytest                             # 111 passed, 2 deselected

# real network smoke test (uses local .env credentials)
D:/anaconda3/python.exe -m pytest -m llm_network -v           # 1 passed, 112 deselected

# Phase 2 acceptance command (real call)
D:/anaconda3/python.exe main.py llm \
  --prompt "Explain the main biological function of TP53 in one sentence."

# error path with credentials disabled
D:/anaconda3/python.exe -c "... config._env_file_loaded = True; GLMClient()"   # → LLMConfigurationError
```

## Actual GLM result

- Command: the acceptance `llm --prompt ...` above.
- Success: yes (exit 0).
- Model: `glm-5.3` (from local `GLM_MODEL`).
- Response (abbreviated): "TP53 encodes the tumor suppressor protein p53,
  which acts as a cellular stress sensor—responding to DNA damage,
  oncogene activation, and other stresses by inducing cell cycle arrest,
  DNA repair, senescence, or apoptosis…".
- Usage: prompt tokens=34, completion tokens=197, total tokens=231.
- No key material appeared in any output.

## Problems encountered

### Problem 1 — SDK class attribute not inspectable before instantiation

```text
problem   `ZhipuAI.chat` could not be inspected on the class (AttributeError),
          almost leading to the conclusion that the installed SDK was legacy
cause     `chat` is bound per-instance in __init__ (OpenAI-SDK style), not on
          the class
fix       instantiate ZhipuAI(api_key='dummy') first, then inspect
          client.chat.completions.create and the exported error classes
reason    confirm the current v2 API surface (create params, max_retries,
          exception types) before writing the adapter against it
result    adapter written against verified signatures; no dead code
```

### Problem 2 — real `.env` could leak into offline tests

```text
problem   the developer's real .env contains working GLM credentials; the
          "missing key" / "missing model" offline tests would have read them
          via load_env_file and failed (or worse, passed non-hermetically)
cause     the shared .env loader is process-wide by design
fix       the test fixture monkeypatches app.llm.glm.load_env_file to a
          no-op and sets its own env vars; the smoke test is the only place
          the real .env is consulted
reason    offline tests must be hermetic and must never depend on (or print)
          real secrets
result    deterministic offline suite; real credentials used only by the
          opt-in network test
```

No code defects surfaced during the offline test runs (111/111 on the
first full run); both problems above were discovered during
inspection/design, before they could bite.

## Current limitations

- No structured output / JSON mode — `chat` returns free text only.
- No tool calling — the model cannot invoke `search_pubmed` etc.
- No agent loop, state, or planning; no connection between GLM and the
  PubMed tool or GeneRecords (three independent components).
- No conversation persistence — each `chat` call is stateless.
- No streaming (fixed `stream=False`).
- Single model per call, no batching/caching, and no token accounting
  across calls.
- No HTML report and no UI.
