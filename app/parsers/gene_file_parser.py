"""Gene / differential-analysis file parser (Phase 1).

Pipeline implemented here::

    parse_gene_file(path)
        file (.csv/.tsv/.txt/.xlsx)
        → pandas DataFrame
        → deterministic column mapping (alias table, no LLM)
        → row-by-row cleaning + validation
        → duplicate policy (keep first occurrence)
        → GeneParseResult (records + statistics)

Column names are matched case-insensitively after trimming whitespace; if
more than one column matches the same canonical field the parser raises
instead of silently picking one. Statistics (missing genes, duplicates,
invalid numeric cells) are counted so the CLI / future HTML report can show
what was dropped and why.

Species detection, gene-symbol validation, Ensembl ID conversion and any
database access are explicitly out of scope for Phase 1.
"""

from __future__ import annotations

import math
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from app.models.schemas import GeneRecord

SUPPORTED_SUFFIXES = (".csv", ".tsv", ".txt", ".xlsx")

# Canonical field -> accepted column names, in lowercased/trimmed form.
# Extended deliberately conservatively: only spellings that tools actually
# emit (DESeq2, edgeR, limma, Excel exports) are listed.
COLUMN_ALIASES: dict[str, set[str]] = {
    "gene": {
        "gene", "symbol", "gene_symbol", "genesymbol",
    },
    "logfc": {
        "logfc", "log2fc", "log2foldchange", "avg_logfc", "avg_log2fc",
    },
    "pvalue": {
        "pvalue", "p_value", "pval", "p.value",
    },
    "padj": {
        "padj", "fdr", "qvalue", "q_value", "adj.p.val", "adjusted_pvalue",
    },
}

# Fields that must lie in [0, 1]; anything outside is invalid (never clamped).
_PROBABILITY_FIELDS = ("pvalue", "padj")


class GeneFileError(RuntimeError):
    """Raised when a gene file cannot be read, understood, or mapped.

    Messages are user-facing and self-diagnosing (they list actual column
    names and the supported aliases where relevant).
    """


@dataclass
class GeneParseResult:
    """Successful parse outcome: cleaned records + audit statistics.

    Kept as a plain dataclass next to the parser (it describes the parse
    itself, so it lives with the parsing code rather than in the shared
    ``app.models.schemas`` module).

    Counting scope: ``invalid_numeric_count`` covers only statistic cells in
    **retained unique-gene rows**. Rows dropped earlier (missing gene, or
    duplicate gene under the keep-first policy) are skipped before numeric
    parsing, so their cells are never inspected or counted. The other
    counters (``total_rows``, ``missing_gene_count``,
    ``duplicate_gene_count``) do cover all input rows.
    """

    records: list[GeneRecord] = field(default_factory=list)
    source_file: str = ""
    total_rows: int = 0
    valid_gene_rows: int = 0
    unique_genes: int = 0
    duplicate_gene_count: int = 0
    missing_gene_count: int = 0
    # Invalid cells in retained rows only — see class docstring.
    invalid_numeric_count: int = 0
    column_mapping: dict[str, str] = field(default_factory=dict)
    duplicate_gene_names: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_gene_file(
    path: str | Path,
    sheet_name: int | str = 0,
) -> GeneParseResult:
    """Parse a gene/DEG file into cleaned :class:`GeneRecord` records.

    Args:
        path: ``.csv`` / ``.tsv`` / ``.txt`` (tab-separated) / ``.xlsx`` file.
        sheet_name: Excel sheet index or name; only used for ``.xlsx``
            (default: first sheet).

    Returns:
        :class:`GeneParseResult` with records (duplicates collapsed to their
        first occurrence) and full audit statistics.

    Raises:
        GeneFileError: file missing, unsupported suffix, unreadable content,
            unparseable table, no recognizable gene column, or an ambiguous
            column mapping.
    """
    file_path = Path(path)
    _check_readable(file_path)
    dataframe = _read_dataframe(file_path, sheet_name)
    mapping = _map_columns(dataframe, file_path)
    return _build_records(dataframe, mapping, str(file_path))


# ---------------------------------------------------------------------------
# File reading
# ---------------------------------------------------------------------------

def _check_readable(path: Path) -> None:
    if not path.exists():
        raise GeneFileError(f"File not found: {path}")
    if path.is_dir():
        raise GeneFileError(f"Path is a directory, not a file: {path}")
    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise GeneFileError(
            f"Unsupported file type '{path.suffix or '(none)'}': {path}. "
            f"Supported: {', '.join(SUPPORTED_SUFFIXES)}"
        )


def _read_dataframe(path: Path, sheet_name: int | str) -> pd.DataFrame:
    suffix = path.suffix.lower()
    try:
        if suffix == ".csv":
            return pd.read_csv(path, encoding="utf-8-sig")
        if suffix in (".tsv", ".txt"):
            # Phase 1 treats plain .txt as a tab-separated table.
            return pd.read_csv(path, sep="\t", encoding="utf-8-sig")
        return pd.read_excel(path, sheet_name=sheet_name, engine="openpyxl")
    except UnicodeDecodeError as exc:
        raise GeneFileError(
            f"Could not decode {path} as UTF-8 ({exc}). "
            "Please re-save the file as UTF-8."
        ) from exc
    except (
        pd.errors.ParserError,
        pd.errors.EmptyDataError,
        zipfile.BadZipFile,
        ValueError,
        KeyError,
    ) as exc:
        # ValueError/KeyError cover openpyxl's invalid-file and missing-sheet
        # failures; BadZipFile covers non-Excel files named *.xlsx.
        raise GeneFileError(
            f"Could not parse {path} as a "
            f"{'Excel workbook' if suffix == '.xlsx' else 'table'}: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Column mapping
# ---------------------------------------------------------------------------

def _normalize_column(name: object) -> str:
    return str(name).strip().lower()


def _map_columns(dataframe: pd.DataFrame, path: Path) -> dict[str, str]:
    """Map canonical field names (gene/logfc/pvalue/padj) to real columns.

    Matching is deterministic: trim + lowercase the header, then look it up
    in :data:`COLUMN_ALIASES`. All original columns are examined
    individually (not collapsed into a dict keyed by their normalized
    form), so distinct headers that normalize to the same key — e.g.
    ``Gene`` and ``gene``, or ``FDR`` and ``fdr`` — both count as matches
    and trigger the ambiguity error. Every canonical field must match at
    most one column; zero matches is only acceptable for the optional
    statistics.
    """
    normalized_columns = [
        (_normalize_column(name), name) for name in dataframe.columns
    ]
    mapping: dict[str, str] = {}
    for canonical, aliases in COLUMN_ALIASES.items():
        matches = [
            original for key, original in normalized_columns if key in aliases
        ]
        if len(matches) > 1:
            raise GeneFileError(
                f"Ambiguous columns for '{canonical}' in {path}: "
                f"{matches}. Please keep only one of them or rename the "
                "others so each field matches exactly one column."
            )
        if matches:
            mapping[canonical] = matches[0]
    if "gene" not in mapping:
        raise GeneFileError(
            f"No gene column found in {path}. Actual columns: "
            f"{[str(name) for name in dataframe.columns]}. "
            f"Supported gene aliases: {sorted(COLUMN_ALIASES['gene'])}. "
            "Note: .txt files are read as tab-separated; if your file uses "
            "commas, rename it to .csv."
        )
    return mapping


# ---------------------------------------------------------------------------
# Row cleaning and validation
# ---------------------------------------------------------------------------

def _clean_gene(value: object) -> str | None:
    """Return the cleaned symbol or ``None`` when the cell is unusable.

    Preserves the original casing (mouse ``Trp53`` must survive). NaN and
    blank strings are both treated as "no gene".
    """
    if pd.isna(value):
        return None
    text = str(value).strip()
    return text or None


def _parse_stat_cell(
    value: object, field: str
) -> tuple[float | None, bool]:
    """Convert one statistic cell to ``(float | None, invalid_flag)``.

    Rules (documented in docs/data-contract.md):
    - NaN / empty string -> ``(None, False)``  (missing, not invalid)
    - non-numeric string or non-finite number -> ``(None, True)``
    - pvalue/padj outside [0, 1] -> ``(None, True)`` (never clamped)
    """
    if pd.isna(value):
        return None, False
    if isinstance(value, bool):
        return None, True
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value).strip()
        if not text:
            return None, False
        try:
            number = float(text)
        except ValueError:
            return None, True
    if not math.isfinite(number):
        return None, True
    if field in _PROBABILITY_FIELDS and not 0.0 <= number <= 1.0:
        return None, True
    return number, False


def _build_records(
    dataframe: pd.DataFrame,
    mapping: dict[str, str],
    source_file: str,
) -> GeneParseResult:
    result = GeneParseResult(source_file=source_file, column_mapping=mapping)
    gene_column = mapping["gene"]
    stat_columns = {
        field: mapping[field] for field in ("logfc", "pvalue", "padj") if field in mapping
    }
    seen_genes: set[str] = set()

    for row in dataframe.itertuples(index=False):
        result.total_rows += 1
        row_dict = dict(zip(dataframe.columns, row))

        gene = _clean_gene(row_dict[gene_column])
        if gene is None:
            result.missing_gene_count += 1
            continue
        result.valid_gene_rows += 1
        if gene in seen_genes:
            result.duplicate_gene_count += 1
            if gene not in result.duplicate_gene_names:
                result.duplicate_gene_names.append(gene)
            continue  # duplicate policy: keep first occurrence, no aggregation
        seen_genes.add(gene)

        stats: dict[str, float | None] = {}
        for field, column in stat_columns.items():
            number, invalid = _parse_stat_cell(row_dict[column], field)
            if invalid:
                result.invalid_numeric_count += 1
            stats[field] = number

        result.records.append(
            GeneRecord(
                gene=gene,
                logfc=stats.get("logfc"),
                pvalue=stats.get("pvalue"),
                padj=stats.get("padj"),
            )
        )

    result.unique_genes = len(result.records)
    return result
