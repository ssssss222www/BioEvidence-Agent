# Literature Agent

## Project

Literature Agent — gene-list-driven biomedical literature research agent.

## Goal

Given a gene list (or a differential-expression result file), the finished
agent will retrieve PubMed literature and gene/pathway knowledge, plan
retrieval tasks with an LLM, rank and verify the evidence, and produce an
HTML biology report with PMID-backed citations.

## Current status

```text
Phase 3 — Structured Output
```

(Phases 0-2 are complete and still available as the `pubmed` / `parse` /
`llm` subcommands.)

## Current workflows

Four independent capabilities so far — deliberately not connected yet:

```text
Phase 0:                Phase 1:                Phase 2:        Phase 3:
PubMed query            Input file              Messages        User intent (text)
→ PubMed Tool           → File Parser           → GLM Client    → GLM + JSON + Pydantic
→ Article[]             → GeneRecord[]          → LLMResponse   → SearchIntent
```

## Installation

Requires Python >= 3.11.

```bash
python -m venv .venv
```

Activate the virtual environment:

- Windows (PowerShell): `.\.venv\Scripts\Activate.ps1`
- Windows (cmd): `.venv\Scripts\activate.bat`
- Linux / macOS: `source .venv/bin/activate`

Then install dependencies:

```bash
pip install -r requirements.txt
```

Optional but required for Phase 2: copy `.env.example` to `.env` and set
`GLM_API_KEY` / `GLM_MODEL` (ZhipuAI GLM credentials — see
https://open.bigmodel.cn/). `NCBI_EMAIL` / `NCBI_API_KEY` stay optional
for the PubMed tool. `.env` is git-ignored; never commit real keys.

## Usage

CLI uses subcommands as of Phase 1.

Search PubMed (Phase 0):

```bash
python main.py pubmed --query "TP53 AND breast cancer" --max-results 5
```

> **Breaking change (Phase 1):** the Phase 0 form
> `python main.py --query "..."` no longer works; add the `pubmed`
> subcommand as shown above.

Parse a gene / differential-analysis file (Phase 1):

```bash
python main.py parse --input examples/example_deg.csv
```

Supports `.csv`, `.tsv`, `.txt` (tab-separated) and `.xlsx` (first sheet
by default, `--sheet-name` to select another). The parser auto-detects the
gene column and optional `logFC` / `pvalue` / `padj` columns via a
deterministic alias table, cleans gene symbols (whitespace only — casing is
preserved), keeps the first occurrence of duplicate genes, and reports
missing/invalid cells as statistics. Output:
`outputs/gene_records.json`.

Example files: `examples/example_deg.csv` (intentionally dirty),
`examples/example_queries.txt`.

Call the GLM chat API (Phase 2; needs `GLM_API_KEY` + `GLM_MODEL`):

```bash
python main.py llm --prompt "Explain the main biological function of TP53 in one sentence."
```

Optional flags: `--system "..."` (default: a simple biomedical-assistant
persona), `--model ...` (overrides `GLM_MODEL`), `--temperature 0.1`
(omitted → API-side default). Prints provider, model, response text and
token usage; never prints the API key.

Extract structured data from free text (Phase 3):

```bash
python main.py structured --prompt "I want to investigate TP53 in breast cancer, focusing on DNA damage and apoptosis."
```

Sends the text to GLM with a strict extraction prompt and JSON mode,
parses the reply as JSON, validates it against the `SearchIntent` Pydantic
schema, prints the result and saves it (plus model/usage metadata) to
`outputs/search_intent.json`. Invalid JSON or schema violations fail
loudly (`[structured error]`) — no silent repair. `--model` /
`--temperature` / `--output` flags available.

Run the tests (offline by default; live-API smoke tests are excluded):

```bash
python -m pytest
```

Run only the real-network smoke tests:

```bash
python -m pytest -m network        # NCBI E-utilities
python -m pytest -m llm_network    # ZhipuAI GLM (needs GLM_API_KEY/GLM_MODEL)
```

## Roadmap

```text
Phase 3 Structured output       ← current
Phase 4 Tool calling / Agent loop
Phase 5 NCBI Gene + Reactome
Phase 6 Evidence ranking
Phase 7 Citation verification
Phase 8 HTML report
Phase 9 UI
```

## Documentation

- `docs/architecture.md` — project layout and data flow
- `docs/data-contract.md` — inputs/outputs of every public function
- `docs/development-log.md` — change log and rationale
- `docs/phase-00-pubmed-retrieval.md` — Phase 0 execution record
- `docs/phase-01-gene-file-parser.md` — Phase 1 execution record
- `docs/phase-02-glm-client.md` — Phase 2 execution record
- `docs/phase-03-structured-output.md` — Phase 3 execution record
