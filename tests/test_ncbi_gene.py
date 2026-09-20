"""Offline tests for the Phase 5 NCBI Gene tool.

All HTTP is faked by monkeypatching ``app.tools.ncbi_gene._http_get``;
fixtures mirror the real ESearch/ESummary JSON verified on 2026-09-20
(TP53 + human → GeneID 7157).
"""

from __future__ import annotations

import json

import pytest

import app.tools.ncbi_gene as ncbi_gene
from app.tools.ncbi_gene import NCBIGeneError, get_gene_info

ESEARCH_TP53 = {"esearchresult": {"count": "1", "idlist": ["7157"]}}


def docsum_tp53(uid="7157", symbol="TP53", scientific="Homo sapiens", taxid=9606):
    return {
        "name": symbol,
        "description": "tumor protein p53",
        "chromosome": "17",
        "maplocation": "17p13.1",
        "summary": "This gene encodes a tumor suppressor protein.",
        "otheraliases": "BCC7, BMFS5, LFS1, P53, TRP53",
        "organism": {
            "scientificname": scientific,
            "commonname": "human",
            "taxid": taxid,
        },
    }


def esummary_payload(docsums: dict[str, dict]) -> str:
    return json.dumps({"result": {"uids": list(docsums), **docsums}})


@pytest.fixture
def http(monkeypatch):
    """Route fake bodies per E-utilities URL; record requests."""
    state = {"esearch": None, "esummary": None, "requests": [], "error": None}

    def fake_get(url, params):
        state["requests"].append({"url": url, "params": params})
        if state["error"] is not None:
            raise state["error"]
        if url == ncbi_gene.ESEARCH_URL:
            body = state["esearch"] if state["esearch"] is not None else ESEARCH_TP53
            return json.dumps(body)
        body = (
            state["esummary"]
            if state["esummary"] is not None
            else esummary_payload({"7157": docsum_tp53()})
        )
        return body

    monkeypatch.setattr(ncbi_gene, "_http_get", fake_get)
    return state


# ---------------------------------------------------------------------------
# normal path
# ---------------------------------------------------------------------------

def test_exact_tp53_parse(http):
    info = get_gene_info("TP53", "human")
    assert info.gene_id == "7157"
    assert info.symbol == "TP53"
    assert info.name == "tumor protein p53"
    assert info.organism == "Homo sapiens"
    assert info.tax_id == "9606"
    assert info.chromosome == "17"
    assert info.map_location == "17p13.1"
    assert info.aliases == ["BCC7", "BMFS5", "LFS1", "P53", "TRP53"]
    assert info.summary and info.summary.startswith("This gene encodes")
    assert info.ncbi_url == "https://www.ncbi.nlm.nih.gov/gene/7157"


def test_request_query_correctness(http):
    get_gene_info("TP53", "human")
    esearch = http["requests"][0]
    assert esearch["url"] == ncbi_gene.ESEARCH_URL
    assert esearch["params"]["db"] == "gene"
    assert esearch["params"]["term"] == "TP53[sym] AND human[orgn]"
    esummary = http["requests"][1]
    assert esummary["url"] == ncbi_gene.ESUMMARY_URL
    assert esummary["params"]["id"] == "7157"
    assert esummary["params"]["db"] == "gene"


def test_gene_and_species_are_stripped_and_required(http):
    get_gene_info("  TP53 ", " human ")
    assert http["requests"][0]["params"]["term"] == "TP53[sym] AND human[orgn]"
    with pytest.raises(NCBIGeneError, match="gene"):
        get_gene_info("   ", "human")
    with pytest.raises(NCBIGeneError, match="species"):
        get_gene_info("TP53", "")


# ---------------------------------------------------------------------------
# resolution policy
# ---------------------------------------------------------------------------

def test_no_result_raises(http):
    http["esearch"] = {"esearchresult": {"count": "0", "idlist": []}}
    with pytest.raises(NCBIGeneError, match="no NCBI Gene record"):
        get_gene_info("ZZZNOTAGENE", "human")


def test_non_exact_symbol_records_filtered_out(http):
    # ESearch returned records, but none carries the exact requested symbol
    # (Trp53 ≠ TP53 even case-insensitively) -> explicit error.
    http["esummary"] = esummary_payload(
        {"1": docsum_tp53(uid="1", symbol="Trp53", scientific="Mus musculus",
                          taxid=10090)}
    )
    with pytest.raises(NCBIGeneError, match="none with the exact symbol"):
        get_gene_info("TP53", "human")


def test_case_insensitive_lookup_returns_official_casing(http):
    """Symbol matching is case-insensitive (casefold), but the returned
    GeneInfo always carries NCBI's official casing."""
    # tp53 (lowercase request) -> TP53 (official human symbol)
    http["esearch"] = {"esearchresult": {"count": "1", "idlist": ["7157"]}}
    http["esummary"] = esummary_payload({"7157": docsum_tp53()})
    info = get_gene_info("tp53", "human")
    assert info.symbol == "TP53"
    assert info.gene_id == "7157"
    # term uses the requested spelling; [sym] is case-insensitive server-side
    assert http["requests"][0]["params"]["term"] == "tp53[sym] AND human[orgn]"


def test_case_insensitive_mouse_lookup(http):
    # Trp53 / trp53 (mouse) -> Trp53 (official mouse symbol, GeneID 22059)
    docsum = docsum_tp53(uid="22059", symbol="Trp53",
                         scientific="Mus musculus", taxid=10090)
    docsum["description"] = "transformation related protein 53"
    http["esearch"] = {"esearchresult": {"count": "1", "idlist": ["22059"]}}
    http["esummary"] = esummary_payload({"22059": docsum})
    for requested in ("Trp53", "trp53"):
        info = get_gene_info(requested, "Mus musculus")
        assert info.symbol == "Trp53"
        assert info.gene_id == "22059"
        assert info.organism == "Mus musculus"
        assert info.tax_id == "10090"


def test_casing_ambiguity_still_explicit(http):
    # Two records whose symbols differ only in casing still raise the
    # ambiguity error — never a silent pick.
    http["esearch"] = {"esearchresult": {"count": "2", "idlist": ["1", "2"]}}
    http["esummary"] = esummary_payload(
        {"1": docsum_tp53(uid="1", symbol="TP53"),
         "2": docsum_tp53(uid="2", symbol="Tp53")}
    )
    with pytest.raises(NCBIGeneError, match="ambiguous"):
        get_gene_info("tp53", "human")


def test_multiple_exact_matches_are_ambiguous(http):
    http["esearch"] = {"esearchresult": {"count": "2", "idlist": ["1", "2"]}}
    http["esummary"] = esummary_payload(
        {"1": docsum_tp53(uid="1"), "2": docsum_tp53(uid="2")}
    )
    with pytest.raises(NCBIGeneError, match="ambiguous"):
        get_gene_info("TP53", "human")


def test_exact_symbol_wins_when_other_records_exist(http):
    http["esearch"] = {"esearchresult": {"count": "2", "idlist": ["7157", "9"]}}
    http["esummary"] = esummary_payload(
        {"7157": docsum_tp53(), "9": docsum_tp53(uid="9", symbol="TP53-AS1")}
    )
    assert get_gene_info("TP53", "human").gene_id == "7157"


# ---------------------------------------------------------------------------
# malformed / failure responses
# ---------------------------------------------------------------------------

def test_malformed_esearch_json(http, monkeypatch):
    monkeypatch.setattr(
        ncbi_gene, "_http_get", lambda url, params: "<html>not json"
    )
    with pytest.raises(NCBIGeneError, match="malformed JSON"):
        get_gene_info("TP53", "human")


def test_malformed_esummary_shape(http, monkeypatch):
    def fake_get(url, params):
        if url == ncbi_gene.ESEARCH_URL:
            return json.dumps(ESEARCH_TP53)
        return json.dumps({})  # ESummary body missing 'result'

    monkeypatch.setattr(ncbi_gene, "_http_get", fake_get)
    with pytest.raises(NCBIGeneError, match="result.uids"):
        get_gene_info("TP53", "human")


def test_esummary_missing_record(http, monkeypatch):
    def fake_get(url, params):
        if url == ncbi_gene.ESEARCH_URL:
            return json.dumps(ESEARCH_TP53)
        return json.dumps({"result": {"uids": ["7157"]}})  # no record body

    monkeypatch.setattr(ncbi_gene, "_http_get", fake_get)
    with pytest.raises(NCBIGeneError, match="malformed"):
        get_gene_info("TP53", "human")


def test_network_error_propagates(http):
    http["error"] = NCBIGeneError("HTTP 503 from esearch (attempt 3/3)")
    with pytest.raises(NCBIGeneError, match="HTTP 503"):
        get_gene_info("TP53", "human")


# ---------------------------------------------------------------------------
# optional fields
# ---------------------------------------------------------------------------

def test_summary_and_aliases_optional(http):
    docsum = docsum_tp53()
    del docsum["summary"]
    del docsum["otheraliases"]
    http["esummary"] = esummary_payload({"7157": docsum})
    info = get_gene_info("TP53", "human")
    assert info.summary is None
    assert info.aliases == []


def test_organism_missing_tolerated(http):
    docsum = docsum_tp53()
    del docsum["organism"]
    http["esummary"] = esummary_payload({"7157": docsum})
    info = get_gene_info("TP53", "human")
    assert info.organism is None
    assert info.tax_id is None
    assert info.symbol == "TP53"  # core identity still parsed
