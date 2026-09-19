"""Offline tests for the Phase 1 gene-file parser.

All tests run against temporary files created in ``tmp_path`` — no network,
no real databases. XLSX fixtures are written with pandas/openpyxl.
"""

from __future__ import annotations

import pandas as pd
import pytest

from app.parsers.gene_file_parser import (
    COLUMN_ALIASES,
    GeneFileError,
    parse_gene_file,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

STANDARD_HEADER = "gene\tlogFC\tpvalue\tpadj"


def write_table(tmp_path, filename: str, header: str, rows: list[str]):
    """Write a tab- or comma-separated table depending on the file suffix."""
    path = tmp_path / filename
    sep = "," if filename.endswith(".csv") else "\t"
    content = sep.join(header.split("\t")) + "\n"
    content += "\n".join(sep.join(row.split("\t")) for row in rows) + "\n"
    path.write_text(content, encoding="utf-8")
    return path


def write_xlsx(tmp_path, filename: str, dataframe: pd.DataFrame):
    path = tmp_path / filename
    dataframe.to_excel(path, index=False, engine="openpyxl")
    return path


# ---------------------------------------------------------------------------
# File types
# ---------------------------------------------------------------------------

def test_parse_csv(tmp_path):
    path = write_table(
        tmp_path, "deg.csv", STANDARD_HEADER, ["TP53\t1.5\t0.01\t0.05"]
    )
    result = parse_gene_file(path)
    assert [r.gene for r in result.records] == ["TP53"]
    assert result.records[0].logfc == 1.5


def test_parse_tsv(tmp_path):
    path = write_table(
        tmp_path, "deg.tsv", STANDARD_HEADER, ["BRCA1\t-2.0\t0.02\t0.06"]
    )
    result = parse_gene_file(path)
    assert [r.gene for r in result.records] == ["BRCA1"]
    assert result.records[0].logfc == -2.0


def test_parse_txt_is_tab_separated(tmp_path):
    path = write_table(
        tmp_path, "deg.txt", STANDARD_HEADER, ["MYC\t3.0\t0.001\t0.01"]
    )
    result = parse_gene_file(path)
    assert [r.gene for r in result.records] == ["MYC"]


def test_parse_txt_with_commas_gives_actionable_error(tmp_path):
    # A comma-separated file named .txt parses as a single column, which must
    # surface as "no gene column" listing that column, not as a crash.
    path = tmp_path / "deg.txt"
    path.write_text("GeneSymbol,logFC\nTP53,1.5\n", encoding="utf-8")
    with pytest.raises(GeneFileError, match="No gene column found"):
        parse_gene_file(path)


def test_parse_xlsx(tmp_path):
    dataframe = pd.DataFrame(
        {"gene": ["TP53", "BRCA1"], "logFC": [1.5, -2.0], "pvalue": [0.01, 0.02]}
    )
    path = write_xlsx(tmp_path, "deg.xlsx", dataframe)
    result = parse_gene_file(path)
    assert [r.gene for r in result.records] == ["TP53", "BRCA1"]
    assert result.records[0].padj is None  # column absent entirely


def test_parse_xlsx_specific_sheet(tmp_path):
    first = pd.DataFrame({"gene": ["TP53"], "logFC": [1.5]})
    second = pd.DataFrame({"gene": ["BRCA1"], "logFC": [-2.0]})
    path = tmp_path / "deg.xlsx"
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        first.to_excel(writer, sheet_name="sheet_one", index=False)
        second.to_excel(writer, sheet_name="sheet_two", index=False)
    result = parse_gene_file(path, sheet_name="sheet_two")
    assert [r.gene for r in result.records] == ["BRCA1"]


def test_unsupported_suffix_raises(tmp_path):
    path = tmp_path / "deg.parquet"
    path.write_text("gene,logFC\nTP53,1.5\n", encoding="utf-8")
    with pytest.raises(GeneFileError, match="Unsupported file type"):
        parse_gene_file(path)


def test_missing_file_raises(tmp_path):
    with pytest.raises(GeneFileError, match="not found"):
        parse_gene_file(tmp_path / "nope.csv")


def test_truly_empty_file_raises(tmp_path):
    path = tmp_path / "empty.csv"
    path.write_text("", encoding="utf-8")
    with pytest.raises(GeneFileError, match="Could not parse"):
        parse_gene_file(path)


def test_directory_input_raises(tmp_path):
    with pytest.raises(GeneFileError, match="directory"):
        parse_gene_file(tmp_path)


# ---------------------------------------------------------------------------
# gene column recognition
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("alias", ["gene", "Gene", "GENE", "symbol", "Symbol",
                                   "gene_symbol", "GeneSymbol", "geneSymbol"])
def test_gene_column_aliases(tmp_path, alias):
    path = write_table(tmp_path, "deg.tsv", f"{alias}\tlogFC", ["TP53\t1.5"])
    result = parse_gene_file(path)
    assert result.column_mapping["gene"] == alias
    assert result.records[0].gene == "TP53"


def test_gene_column_with_padding_and_case(tmp_path):
    # header whitespace is trimmed before matching; original name is reported
    path = write_table(tmp_path, "deg.tsv", "  GeneSymbol \tlogFC", ["TP53\t1.5"])
    result = parse_gene_file(path)
    assert result.column_mapping["gene"] == "  GeneSymbol "


def test_no_gene_column_error_lists_columns_and_aliases(tmp_path):
    path = write_table(tmp_path, "deg.tsv", "id\tlogFC\tpvalue", ["1\t1.5\t0.01"])
    with pytest.raises(GeneFileError) as excinfo:
        parse_gene_file(path)
    message = str(excinfo.value)
    assert "'id'" in message and "'logFC'" in message  # actual columns
    assert "gene_symbol" in message  # supported aliases


def test_multiple_gene_candidates_raise(tmp_path):
    path = write_table(tmp_path, "deg.tsv", "gene\tGeneSymbol\tlogFC",
                       ["TP53\tTP53\t1.5"])
    with pytest.raises(GeneFileError, match="Ambiguous columns for 'gene'"):
        parse_gene_file(path)


@pytest.mark.parametrize(
    ("columns", "ambiguous_field"),
    [
        (("Gene", "gene"), "gene"),          # same key after normalization
        (("gene", "GeneSymbol"), "gene"),    # two different gene aliases
        (("gene", "FDR", "fdr"), "padj"),    # same-key stat columns
    ],
)
def test_normalized_duplicates_raise_ambiguity(tmp_path, columns, ambiguous_field):
    # Distinct headers that collide after strip().lower() must both count
    # as matches — no silent pick, no dict-key overwrite.
    header = "\t".join(columns)
    row = "\t".join(["TP53"] + ["1.0"] * (len(columns) - 1))
    path = write_table(tmp_path, "deg.tsv", header, [row])
    with pytest.raises(GeneFileError, match=f"Ambiguous columns for '{ambiguous_field}'"):
        parse_gene_file(path)


def test_multiple_stat_candidates_raise(tmp_path):
    path = write_table(tmp_path, "deg.tsv", "gene\tlogFC\tlog2FoldChange",
                       ["TP53\t1.5\t2.0"])
    with pytest.raises(GeneFileError, match="Ambiguous columns for 'logfc'"):
        parse_gene_file(path)


# ---------------------------------------------------------------------------
# gene cleaning
# ---------------------------------------------------------------------------

def test_gene_surrounding_whitespace_stripped(tmp_path):
    path = write_table(tmp_path, "deg.tsv", STANDARD_HEADER,
                       [" TP53 \t1.5\t0.01\t0.05"])
    assert parse_gene_file(path).records[0].gene == "TP53"


def test_missing_gene_nan_counted(tmp_path):
    path = write_table(tmp_path, "deg.tsv", STANDARD_HEADER,
                       ["\t1.5\t0.01\t0.05", "TP53\t1.0\t0.02\t0.06"])
    result = parse_gene_file(path)
    assert result.missing_gene_count == 1
    assert [r.gene for r in result.records] == ["TP53"]


def test_blank_gene_counted_as_missing(tmp_path):
    path = write_table(tmp_path, "deg.tsv", STANDARD_HEADER,
                       ["   \t1.5\t0.01\t0.05"])
    result = parse_gene_file(path)
    assert result.missing_gene_count == 1
    assert result.records == []


def test_duplicate_gene_keeps_first(tmp_path):
    path = write_table(tmp_path, "deg.tsv", STANDARD_HEADER,
                       ["TP53\t1.5\t0.01\t0.05",
                        "TP53\t9.9\t0.9\t0.9",
                        "BRCA1\t-1.0\t0.02\t0.06"])
    result = parse_gene_file(path)
    assert [r.gene for r in result.records] == ["TP53", "BRCA1"]
    kept = result.records[0]
    assert (kept.logfc, kept.pvalue, kept.padj) == (1.5, 0.01, 0.05)
    assert result.duplicate_gene_count == 1
    assert result.duplicate_gene_names == ["TP53"]
    assert result.unique_genes == 2
    assert result.valid_gene_rows == 3  # duplicates still have a valid gene


def test_duplicate_names_unique_in_report(tmp_path):
    path = write_table(tmp_path, "deg.tsv", STANDARD_HEADER,
                       ["TP53\t1.5\t0.01\t0.05", "TP53\t1.6\t0.01\t0.05",
                        "TP53\t1.7\t0.01\t0.05"])
    result = parse_gene_file(path)
    assert result.duplicate_gene_count == 2
    assert result.duplicate_gene_names == ["TP53"]


def test_gene_case_preserved(tmp_path):
    path = write_table(tmp_path, "deg.tsv", STANDARD_HEADER,
                       ["Trp53\t1.5\t0.01\t0.05"])
    assert parse_gene_file(path).records[0].gene == "Trp53"


# ---------------------------------------------------------------------------
# numeric handling
# ---------------------------------------------------------------------------

def test_normal_logfc_negative_and_scientific(tmp_path):
    path = write_table(tmp_path, "deg.tsv", STANDARD_HEADER,
                       ["A\t-1.25\t1e-05\t0.001", "B\t0.0\t0.5\t0.5"])
    records = parse_gene_file(path).records
    assert records[0].logfc == -1.25
    assert records[0].pvalue == 1e-05
    assert records[1].logfc == 0.0


def test_missing_logfc_empty_cell(tmp_path):
    path = write_table(tmp_path, "deg.tsv", STANDARD_HEADER,
                       ["MYC\t\t0.2\t0.25"])
    result = parse_gene_file(path)
    assert result.records[0].logfc is None
    assert result.invalid_numeric_count == 0  # missing is not invalid


def test_missing_pvalue_column_entirely(tmp_path):
    path = write_table(tmp_path, "deg.tsv", "gene\tlogFC", ["TP53\t1.5"])
    result = parse_gene_file(path)
    assert result.records[0].pvalue is None
    assert result.records[0].padj is None
    assert "pvalue" not in result.column_mapping


def test_missing_padj_cell(tmp_path):
    path = write_table(tmp_path, "deg.tsv", STANDARD_HEADER,
                       ["Trp53\t1.2\t0.05\t"])
    assert parse_gene_file(path).records[0].padj is None


def test_non_numeric_string_is_invalid_not_fatal(tmp_path):
    path = write_table(tmp_path, "deg.tsv", STANDARD_HEADER,
                       ["KRAS\tnot_available\t0.4\t0.45",
                        "PTEN\t1.0\t0.4\t0.45"])
    result = parse_gene_file(path)
    assert result.records[0].gene == "KRAS"  # file still parses
    assert result.records[0].logfc is None
    assert result.records[1].logfc == 1.0
    assert result.invalid_numeric_count == 1


def test_infinity_is_invalid(tmp_path):
    path = write_table(tmp_path, "deg.tsv", STANDARD_HEADER,
                       ["A\tinf\t0.1\t0.2"])
    result = parse_gene_file(path)
    assert result.records[0].logfc is None
    assert result.invalid_numeric_count == 1


@pytest.mark.parametrize("bad_value", ["-0.01", "1.2", "2", "-1"])
def test_pvalue_out_of_range_is_invalid(tmp_path, bad_value):
    path = write_table(tmp_path, "deg.tsv", STANDARD_HEADER,
                       [f"A\t1.0\t{bad_value}\t0.2"])
    result = parse_gene_file(path)
    assert result.records[0].pvalue is None  # never clamped
    assert result.invalid_numeric_count == 1


@pytest.mark.parametrize("bad_value", ["-0.5", "1.5"])
def test_padj_out_of_range_is_invalid(tmp_path, bad_value):
    path = write_table(tmp_path, "deg.tsv", STANDARD_HEADER,
                       [f"A\t1.0\t0.1\t{bad_value}"])
    result = parse_gene_file(path)
    assert result.records[0].padj is None
    assert result.invalid_numeric_count == 1


def test_probability_boundaries_are_valid(tmp_path):
    path = write_table(tmp_path, "deg.tsv", STANDARD_HEADER,
                       ["A\t1.0\t0.0\t1.0"])
    result = parse_gene_file(path)
    assert result.records[0].pvalue == 0.0
    assert result.records[0].padj == 1.0
    assert result.invalid_numeric_count == 0


def test_stat_aliases(tmp_path):
    path = write_table(
        tmp_path, "deg.tsv",
        "gene\tlog2FoldChange\tp_value\tadj.P.Val",
        ["TP53\t1.5\t0.01\t0.05"],
    )
    result = parse_gene_file(path)
    assert result.column_mapping == {
        "gene": "gene",
        "logfc": "log2FoldChange",
        "pvalue": "p_value",
        "padj": "adj.P.Val",
    }
    assert result.records[0].logfc == 1.5
    assert result.records[0].padj == 0.05


# ---------------------------------------------------------------------------
# Result statistics
# ---------------------------------------------------------------------------

def test_summary_statistics(tmp_path):
    rows = [
        "TP53\t1.5\t0.01\t0.05",   # normal
        " TP53 \t2.5\t0.01\t0.05",  # duplicate after cleaning
        "BRCA1\t\t0.02\t",         # missing logfc + missing padj cells
        "\t1.0\t0.3\t0.4",         # missing gene
        "Trp53\t1.0\t0.4\t0.45",   # normal, case preserved
    ]
    result = parse_gene_file(write_table(tmp_path, "deg.tsv", STANDARD_HEADER, rows))
    assert result.total_rows == 5
    assert result.missing_gene_count == 1
    assert result.valid_gene_rows == 4
    assert result.duplicate_gene_count == 1
    assert result.unique_genes == 3
    assert result.invalid_numeric_count == 0
    assert result.source_file.endswith("deg.tsv")
    assert [r.gene for r in result.records] == ["TP53", "BRCA1", "Trp53"]


def test_alias_table_contains_required_entries():
    # guard against accidental regressions of the documented alias contract
    required = {
        "gene": {"gene", "symbol", "gene_symbol", "genesymbol"},
        "logfc": {"logfc", "log2fc", "log2foldchange",
                  "avg_logfc", "avg_log2fc"},
        "pvalue": {"pvalue", "p_value", "pval", "p.value"},
        "padj": {"padj", "fdr", "qvalue", "q_value",
                 "adj.p.val", "adjusted_pvalue"},
    }
    for canonical, aliases in required.items():
        assert aliases <= COLUMN_ALIASES[canonical], canonical
