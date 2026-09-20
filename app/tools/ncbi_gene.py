"""NCBI Gene tool (Phase 5).

Pipeline::

    get_gene_info(gene, species)
    → ESearch  db=gene, term="<gene>[sym] AND <species>[orgn]"  → GeneIDs
    → ESummary db=gene                                            → docsums
    → deterministic resolution (exact symbol, exact species)
    → GeneInfo

Why ESummary and not EFetch: the verified ESummary JSON (probe 2026-09-20,
TP53 + human) already provides symbol, full name, organism/taxid,
chromosome, map location, aliases and summary — EFetch's large XML would
add nothing this project uses.

Resolution policy (deterministic, documented):
1. ESearch with exact-symbol + organism field tags keeps the candidate set
   small ("TP53[sym] AND human[orgn]" → exactly one record; the ``[sym]``
   field is case-insensitive on NCBI's side — verified: "tp53[sym] AND
   human[orgn]" → the same record);
2. among the returned docsums, keep only records whose current symbol
   matches the requested gene exactly but **case-insensitively**
   (``casefold()``) — so "tp53" resolves to the official "TP53" and
   "trp53" to the mouse "Trp53". Exact-symbol semantics: no substring or
   fuzzy matching, and species separation still comes from the ``[orgn]``
   field tag (mouse Trp53 and human TP53 stay distinct genes);
3. exactly one match → return it, with the **official ESummary symbol
   casing** (never the user's casing); zero → "no record" error; more than
   one → explicit ambiguity error listing the candidates. Never a silent
   pick.

HTTP engineering mirrors app/tools/pubmed.py (requests + timeout +
User-Agent + bounded transient retries + explicit status/parse errors).
The helper is deliberately duplicated rather than refactored out of
pubmed.py: Phase 0's module — including its test monkeypatch seams — stays
untouched (Phase 5 instruction: no DRY-driven refactoring of PubMed).

NCBI identity (tool/email/api_key) is optional, read via app.config from
the environment / .env — identical policy to the PubMed tool. No API key
is required.
"""

from __future__ import annotations

import json
import os
import time

import requests

from app.config import load_env_file
from app.models.schemas import GeneInfo

ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
ESUMMARY_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"

APP_NAME = "literature-agent"
APP_VERSION = "0.1.0"

REQUEST_TIMEOUT_SECONDS = 30
MAX_RETRIES = 3  # total attempts per request
RETRY_BACKOFF_SECONDS = 1.0
RETRYABLE_HTTP_STATUS = {429, 500, 502, 503, 504}


class NCBIGeneError(RuntimeError):
    """NCBI Gene lookup failed: bad input, network/HTTP, malformed response,
    no record found, or an ambiguous symbol resolution. Messages are
    user-facing and distinguish those cases."""


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

def _validate_non_empty(name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NCBIGeneError(f"{name} must be a non-empty string, got {value!r}")
    return value.strip()


# ---------------------------------------------------------------------------
# HTTP (same bounded policy as the PubMed tool; see module docstring)
# ---------------------------------------------------------------------------

def _ncbi_params() -> dict[str, str]:
    load_env_file()
    params = {"tool": f"{APP_NAME}/{APP_VERSION}"}
    email = os.environ.get("NCBI_EMAIL", "").strip()
    api_key = os.environ.get("NCBI_API_KEY", "").strip()
    if email:
        params["email"] = email
    if api_key:
        params["api_key"] = api_key
    return params


def _http_get(url: str, params: dict[str, str]) -> str:
    """GET one E-utilities endpoint with timeout and bounded retries."""
    headers = {"User-Agent": f"{APP_NAME}/{APP_VERSION}"}
    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(
                url, params=params, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS
            )
        except requests.RequestException as exc:
            last_error = NCBIGeneError(
                f"Network error calling {url}: {exc} "
                f"(attempt {attempt}/{MAX_RETRIES})"
            )
        else:
            if response.status_code == 200:
                return response.text
            reason = f"HTTP {response.status_code} from {url}"
            if response.status_code not in RETRYABLE_HTTP_STATUS:
                raise NCBIGeneError(f"{reason}: {response.text[:200]}")
            last_error = NCBIGeneError(
                f"{reason} (attempt {attempt}/{MAX_RETRIES}): "
                f"{response.text[:200]}"
            )
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    assert last_error is not None
    raise last_error


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_gene_info(gene: str, species: str) -> GeneInfo:
    """Look up one gene in NCBI Gene and return a :class:`GeneInfo`.

    Args:
        gene: gene symbol, e.g. "TP53" (case-sensitive).
        species: organism name for the ``[orgn]`` field, e.g. "human" or
            "Homo sapiens". No implicit default — the caller states it.

    Raises:
        NCBIGeneError: invalid input, network/HTTP failure, malformed
            response, no matching record, or ambiguous resolution.
    """
    gene = _validate_non_empty("gene", gene)
    species = _validate_non_empty("species", species)

    gene_ids = _search_gene_ids(gene, species)
    if not gene_ids:
        raise NCBIGeneError(
            f"no NCBI Gene record found for symbol '{gene}' in species "
            f"'{species}'"
        )
    docsums = _fetch_docsums(gene_ids)

    # Case-insensitive exact match: the requested casing must not leak into
    # the result — the returned GeneInfo always carries the official
    # ESummary symbol casing.
    candidates = [
        docsum
        for docsum in docsums
        if docsum.symbol.casefold() == gene.casefold()
    ]
    if not candidates:
        found = [f"{d.gene_id} ({d.symbol})" for d in docsums]
        raise NCBIGeneError(
            f"NCBI Gene returned records for '{species}' but none with the "
            f"exact symbol '{gene}' (matched case-insensitively): {found}"
        )
    if len(candidates) > 1:
        found = [f"{d.gene_id} ({d.symbol}, {d.organism})" for d in candidates]
        raise NCBIGeneError(
            f"ambiguous gene symbol '{gene}' in species '{species}': "
            f"multiple records match exactly: {found}"
        )
    return candidates[0]


# ---------------------------------------------------------------------------
# ESearch / ESummary steps
# ---------------------------------------------------------------------------

def _search_gene_ids(gene: str, species: str) -> list[str]:
    term = f"{gene}[sym] AND {species}[orgn]"
    body = _http_get(
        ESEARCH_URL,
        {"db": "gene", "term": term, "retmode": "json", **_ncbi_params()},
    )
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise NCBIGeneError(
            f"ESearch returned malformed JSON: {body[:200]!r}"
        ) from exc
    if not isinstance(payload, dict):
        raise NCBIGeneError("ESearch response was not a JSON object")
    error = payload.get("error") or (
        payload.get("header", {}).get("error")
        if isinstance(payload.get("header"), dict)
        else None
    )
    if error:
        raise NCBIGeneError(f"ESearch reported an error: {error}")
    result = payload.get("esearchresult")
    id_list = result.get("idlist") if isinstance(result, dict) else None
    if not isinstance(id_list, list):
        raise NCBIGeneError("ESearch response is missing 'esearchresult.idlist'")
    return [str(gene_id) for gene_id in id_list]


def _fetch_docsums(gene_ids: list[str]) -> list[GeneInfo]:
    body = _http_get(
        ESUMMARY_URL,
        {"db": "gene", "id": ",".join(gene_ids), "retmode": "json", **_ncbi_params()},
    )
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise NCBIGeneError(
            f"ESummary returned malformed JSON: {body[:200]!r}"
        ) from exc
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict) or "uids" not in result:
        raise NCBIGeneError(
            "ESummary response is missing 'result.uids' — malformed response"
        )
    docsums: list[GeneInfo] = []
    for uid in result["uids"]:
        record = result.get(str(uid))
        if not isinstance(record, dict):
            raise NCBIGeneError(f"ESummary record for GeneID {uid} is malformed")
        docsums.append(_gene_info_from_docsum(str(uid), record))
    return docsums


def _gene_info_from_docsum(uid: str, record: dict) -> GeneInfo:
    """Map one ESummary docsum onto :class:`GeneInfo` (missing → None/[])."""
    organism = record.get("organism") if isinstance(record.get("organism"), dict) else {}
    aliases = record.get("otheraliases")
    return GeneInfo(
        gene_id=uid,
        symbol=record.get("name") or "",
        name=record.get("description"),
        organism=organism.get("scientificname"),
        tax_id=str(organism["taxid"]) if organism.get("taxid") is not None else None,
        chromosome=record.get("chromosome"),
        map_location=record.get("maplocation"),
        aliases=[a.strip() for a in aliases.split(",") if a.strip()]
        if isinstance(aliases, str)
        else [],
        summary=record.get("summary") or None,
    )
