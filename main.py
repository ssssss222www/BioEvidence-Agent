"""Literature Agent CLI — orchestration only.

Phase 0 (``pubmed``): PubMed query -> ESearch -> PMIDs -> EFetch -> Article
-> JSON. Phase 1 (``parse``): gene/DEG file -> column mapping -> cleaning
-> GeneRecord -> JSON. Phase 2 (``llm``): prompt -> GLMClient -> LLMResponse
printed to the terminal. Phase 3 (``structured``): free text -> GLMClient ->
JSON -> Pydantic validation -> SearchIntent saved as JSON.

This module wires together argument parsing, the PubMed tool, the gene-file
parser, the GLM client, terminal summary printing and JSON persistence. All
retrieval, parsing and LLM logic lives in ``app.tools.pubmed``,
``app.parsers.gene_file_parser`` and ``app.llm`` so they can be reused
later as Agent tools.

Breaking change in Phase 1: the Phase 0 CLI ``python main.py --query ...``
became ``python main.py pubmed --query ...`` (argparse subcommands).

Usage::

    python main.py pubmed --query "TP53 AND breast cancer" --max-results 5
    python main.py parse  --input examples/example_deg.csv
    python main.py llm    --prompt "Explain the main biological function of TP53 in one sentence."
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.models.schemas import Article
from app.parsers.gene_file_parser import GeneFileError, GeneParseResult, parse_gene_file
from app.tools.pubmed import PubMedError, search_pubmed

DEFAULT_PUBMED_OUTPUT = Path("outputs") / "pubmed_results.json"
DEFAULT_GENE_OUTPUT = Path("outputs") / "gene_records.json"
DEFAULT_INTENT_OUTPUT = Path("outputs") / "search_intent.json"
DEFAULT_LLM_SYSTEM_PROMPT = "You are a precise biomedical research assistant."
ABSTRACT_PREVIEW_CHARS = 160
GENE_PREVIEW_COUNT = 5

# Exit codes documented for scripting.
EXIT_OK = 0
EXIT_TOOL_FAILED = 1
EXIT_BAD_INPUT = 2


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="literature-agent",
        description="Gene-list-driven biomedical literature research agent "
        "(Phase 0: PubMed retrieval, Phase 1: gene file parser, "
        "Phase 2: GLM chat, Phase 3: structured output extraction).",
        epilog='Examples:\n'
        '  python main.py pubmed --query "TP53 AND breast cancer" --max-results 5\n'
        "  python main.py parse --input examples/example_deg.csv\n"
        "  python main.py llm --prompt \"Explain the main biological function of TP53 in one sentence.\"\n"
        "  python main.py structured --prompt \"I want to investigate TP53 in breast cancer, focusing on DNA damage and apoptosis.\"",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subcommands = parser.add_subparsers(dest="command", required=True, metavar="command")

    pubmed_parser = subcommands.add_parser(
        "pubmed", help="Search PubMed and save article metadata as JSON (Phase 0)"
    )
    pubmed_parser.add_argument(
        "--query",
        required=True,
        help='PubMed query, e.g. "TP53 AND breast cancer"',
    )
    pubmed_parser.add_argument(
        "--max-results",
        type=int,
        default=10,
        help="Maximum number of articles to retrieve (1-200, default: %(default)s)",
    )
    pubmed_parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_PUBMED_OUTPUT,
        help=f"Path of the JSON output file (default: {DEFAULT_PUBMED_OUTPUT})",
    )

    parse_parser = subcommands.add_parser(
        "parse", help="Parse a gene/DEG file into GeneRecords and save JSON (Phase 1)"
    )
    parse_parser.add_argument(
        "--input",
        required=True,
        type=Path,
        help="Input file: .csv, .tsv, .txt (tab-separated) or .xlsx",
    )
    parse_parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_GENE_OUTPUT,
        help=f"Path of the JSON output file (default: {DEFAULT_GENE_OUTPUT})",
    )
    parse_parser.add_argument(
        "--sheet-name",
        default="0",
        help="Excel sheet name or index (.xlsx only, default: first sheet)",
    )

    llm_parser = subcommands.add_parser(
        "llm", help="Call the GLM chat API (Phase 2)"
    )
    llm_parser.add_argument(
        "--prompt",
        required=True,
        help="User prompt to send to the model",
    )
    llm_parser.add_argument(
        "--system",
        default=DEFAULT_LLM_SYSTEM_PROMPT,
        help="System prompt (default: a simple biomedical assistant persona)",
    )
    llm_parser.add_argument(
        "--model",
        default=None,
        help="GLM model name (default: GLM_MODEL from environment/.env)",
    )
    llm_parser.add_argument(
        "--temperature",
        type=float,
        default=None,
        help="Sampling temperature in [0, 1] (default: API-side default)",
    )

    structured_parser = subcommands.add_parser(
        "structured",
        help="Extract a SearchIntent (JSON) from free text via GLM (Phase 3)",
    )
    structured_parser.add_argument(
        "--prompt",
        required=True,
        help='Free-text research goal, e.g. "I want to investigate TP53 in '
        'breast cancer, focusing on DNA damage and apoptosis."',
    )
    structured_parser.add_argument(
        "--model",
        default=None,
        help="GLM model name (default: GLM_MODEL from environment/.env)",
    )
    structured_parser.add_argument(
        "--temperature",
        type=float,
        default=None,
        help="Sampling temperature in [0, 1] (default: 0.1, the extraction default)",
    )
    structured_parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_INTENT_OUTPUT,
        help=f"Path of the JSON output file (default: {DEFAULT_INTENT_OUTPUT})",
    )
    return parser


# ---------------------------------------------------------------------------
# pubmed subcommand
# ---------------------------------------------------------------------------

def print_pubmed_summary(query: str, articles: list[Article]) -> None:
    """Print a short human-readable summary of the retrieved articles."""
    print(f"Query: {query}")
    print(f"Retrieved {len(articles)} article(s) from PubMed")
    print("-" * 72)
    for index, article in enumerate(articles, start=1):
        first_author = (
            article.authors[0] if article.authors else "Unknown author"
        )
        if len(article.authors) > 1:
            first_author += " et al."
        year = article.publication_year or "n/a"
        print(f"[{index}] {first_author} ({year}). {article.title or 'No title'}.")
        print(f"    Journal: {article.journal or 'Unknown journal'}")
        print(f"    PMID: {article.pmid} | DOI: {article.doi or 'n/a'}")
        print(f"    URL:   {article.pubmed_url}")
        if article.abstract:
            preview = article.abstract[:ABSTRACT_PREVIEW_CHARS].replace("\n", " ")
            ellipsis = "..." if len(article.abstract) > ABSTRACT_PREVIEW_CHARS else ""
            print(f"    Abstract: {preview}{ellipsis}")
        else:
            print("    Abstract: (none in PubMed)")
    print("-" * 72)


def save_pubmed_json(path: Path, query: str, articles: list[Article]) -> Path:
    """Write the full PubMed result to ``path`` as UTF-8, pretty-printed JSON."""
    payload = {
        "query": query,
        "retrieved_count": len(articles),
        "articles": [article.to_dict() for article in articles],
    }
    return _write_json(path, payload)


def run_pubmed(args: argparse.Namespace) -> int:
    try:
        articles = search_pubmed(args.query, args.max_results)
    except ValueError as exc:
        print(f"[invalid input] {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT
    except PubMedError as exc:
        print(f"[pubmed error] {exc}", file=sys.stderr)
        return EXIT_TOOL_FAILED

    print_pubmed_summary(args.query, articles)
    output_path = save_pubmed_json(args.output, args.query, articles)
    print(f"Full results saved to: {output_path}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# parse subcommand
# ---------------------------------------------------------------------------

def print_parse_summary(result: GeneParseResult) -> None:
    print(f"Source file: {result.source_file}")
    print(
        "Column mapping: "
        + ", ".join(
            f"{canonical} <- '{original}'"
            for canonical, original in result.column_mapping.items()
        )
    )
    print(f"Total rows: {result.total_rows}")
    print(f"Rows with a valid gene: {result.valid_gene_rows}")
    print(f"Unique genes: {result.unique_genes}")
    print(f"Duplicate gene rows dropped (kept first): {result.duplicate_gene_count}")
    if result.duplicate_gene_names:
        print(f"Duplicate gene names: {', '.join(result.duplicate_gene_names)}")
    print(f"Rows with missing/blank gene: {result.missing_gene_count}")
    print(f"Invalid numeric cells (set to null): {result.invalid_numeric_count}")
    if result.invalid_numeric_count:
        print(
            "Warning: some statistic cells were not usable numbers or were "
            "p-values outside [0, 1]; they are stored as null. See "
            "docs/data-contract.md for the exact rules."
        )
    print("-" * 72)
    for record in result.records[:GENE_PREVIEW_COUNT]:
        print(
            f"  {record.gene}: logfc={_fmt(record.logfc)}, "
            f"pvalue={_fmt(record.pvalue)}, padj={_fmt(record.padj)}"
        )
    hidden = len(result.records) - GENE_PREVIEW_COUNT
    if hidden > 0:
        print(f"  ... and {hidden} more (see JSON output for all records)")
    print("-" * 72)


def _fmt(value: float | None) -> str:
    return "null" if value is None else repr(value)


def save_gene_json(path: Path, result: GeneParseResult) -> Path:
    payload = {
        "source_file": result.source_file,
        "summary": {
            "total_rows": result.total_rows,
            "valid_gene_rows": result.valid_gene_rows,
            "unique_genes": result.unique_genes,
            "duplicate_gene_count": result.duplicate_gene_count,
            "missing_gene_count": result.missing_gene_count,
            "invalid_numeric_count": result.invalid_numeric_count,
        },
        "column_mapping": result.column_mapping,
        "duplicate_gene_names": result.duplicate_gene_names,
        "genes": [record.to_dict() for record in result.records],
    }
    return _write_json(path, payload)


def run_parse(args: argparse.Namespace) -> int:
    sheet_name: int | str = (
        int(args.sheet_name) if args.sheet_name.lstrip("-").isdigit() else args.sheet_name
    )
    try:
        result = parse_gene_file(args.input, sheet_name=sheet_name)
    except GeneFileError as exc:
        print(f"[gene file error] {exc}", file=sys.stderr)
        return EXIT_TOOL_FAILED

    print_parse_summary(result)
    output_path = save_gene_json(args.output, result)
    print(f"Full results saved to: {output_path}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# llm subcommand
# ---------------------------------------------------------------------------

def run_llm(args: argparse.Namespace) -> int:
    # Imported lazily so `pubmed` / `parse` keep working even if the zhipuai
    # SDK is absent (it is only needed for this subcommand).
    from app.llm.base import LLMConfigurationError, LLMError
    from app.llm.glm import GLMClient
    from app.models.schemas import LLMResponse

    messages: list[dict[str, str]] = []
    if args.system and args.system.strip():
        messages.append({"role": "system", "content": args.system})
    messages.append({"role": "user", "content": args.prompt})

    try:
        client = GLMClient(model=args.model)
        response: LLMResponse = client.chat(messages, temperature=args.temperature)
    except ValueError as exc:
        print(f"[invalid input] {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT
    except LLMConfigurationError as exc:
        print(f"[llm config error] {exc}", file=sys.stderr)
        return EXIT_TOOL_FAILED
    except LLMError as exc:
        print(f"[llm error] {exc}", file=sys.stderr)
        return EXIT_TOOL_FAILED

    print("Provider: GLM")
    print(f"Model: {response.model}")
    print("Response:")
    print(response.content)
    if response.total_tokens is not None:
        print(
            f"Usage: prompt tokens={response.prompt_tokens} | "
            f"completion tokens={response.completion_tokens} | "
            f"total tokens={response.total_tokens}"
        )
    return EXIT_OK


# ---------------------------------------------------------------------------
# structured subcommand
# ---------------------------------------------------------------------------

def run_structured(args: argparse.Namespace) -> int:
    # Lazy imports keep pubmed/parse working without the zhipuai SDK.
    from app.llm.base import LLMConfigurationError, LLMError
    from app.llm.glm import GLMClient
    from app.llm.structured import StructuredOutputError, extract_search_intent

    try:
        client = GLMClient(model=args.model)
        result = extract_search_intent(
            client, args.prompt, temperature=args.temperature
        )
    except ValueError as exc:
        print(f"[invalid input] {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT
    except StructuredOutputError as exc:
        print(f"[structured error] {exc}", file=sys.stderr)
        return EXIT_TOOL_FAILED
    except LLMConfigurationError as exc:
        print(f"[llm config error] {exc}", file=sys.stderr)
        return EXIT_TOOL_FAILED
    except LLMError as exc:
        print(f"[llm error] {exc}", file=sys.stderr)
        return EXIT_TOOL_FAILED

    intent = result.data
    print("Provider: GLM")
    print(f"Model: {result.raw_response.model}")
    print("Structured result:")
    print(f"  gene: {intent.gene}")
    print(f"  disease: {intent.disease if intent.disease is not None else 'null'}")
    if intent.topics:
        print("  topics:")
        for topic in intent.topics:
            print(f"    - {topic}")
    else:
        print("  topics: []")
    usage = result.raw_response
    if usage.total_tokens is not None:
        print(
            f"Usage: prompt tokens={usage.prompt_tokens} | "
            f"completion tokens={usage.completion_tokens} | "
            f"total tokens={usage.total_tokens}"
        )

    payload = {
        **intent.model_dump(),
        "model": result.raw_response.model,
        "usage": {
            "prompt_tokens": result.raw_response.prompt_tokens,
            "completion_tokens": result.raw_response.completion_tokens,
            "total_tokens": result.raw_response.total_tokens,
        },
    }
    output_path = _write_json(args.output, payload)
    print(f"Saved to: {output_path}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _write_json(path: Path, payload: dict) -> Path:
    """Write ``payload`` to ``path`` as UTF-8, pretty-printed JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def _force_utf8_stdout() -> None:
    """Reconfigure stdout to UTF-8 on Windows (default codepage is often GBK).

    Titles and gene files regularly contain characters that a non-UTF-8
    console codepage cannot encode, which would crash ``print`` mid-summary.
    If reconfiguring fails we simply keep the default and let the normal
    UnicodeEncodeError surface.
    """
    encoding = (sys.stdout.encoding or "").lower().replace("-", "")
    if encoding != "utf8":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdout()
    args = build_parser().parse_args(argv)
    if args.command == "pubmed":
        return run_pubmed(args)
    if args.command == "parse":
        return run_parse(args)
    if args.command == "llm":
        return run_llm(args)
    if args.command == "structured":
        return run_structured(args)
    raise AssertionError(f"unhandled command: {args.command}")  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())
