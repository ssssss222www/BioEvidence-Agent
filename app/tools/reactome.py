"""Reactome pathway tool (Phase 5).

Endpoint choice (verified with a real probe on 2026-09-20): the Reactome
**Analysis Service** ``POST /AnalysisService/identifiers/`` with a plain-text
gene symbol body. It was chosen over the Content Service because it maps a
free-text identifier directly to an ordered pathway list in one call
(stable IDs, names, species, disease flags, pageSize pagination), while the
Content Service expects already-resolved Reactome/external database IDs and
offers no direct symbol→pathway ordered lookup endpoint. Only one
implementation is kept.

Response shape (real, TP53 + Homo sapiens): ``{"pathwaysFound": <int>,
"pathways": [{"stId": "R-HSA-…", "name": …, "species": {"name": …,
"taxId": …}, "inDisease": bool, "entities": {…}, …}], …}``.

Ordering policy: we preserve the API's analysis-result order (verified
stable across identical calls). That order is simply how the Analysis
Service returns its results (its statistical ordering) — it is NOT a
statement of biological importance, causal relevance, or regulatory
importance. We de-duplicate by ``stId`` (keep first) and slice to
``max_results``. No LLM-side "relevance" sorting.

Semantics: a mapping means the gene participates in / is associated with
the curated pathway — never a causal "regulates" claim.
"""

from __future__ import annotations

import json

import requests

from app.models.schemas import ReactomePathway

ANALYSIS_URL = "https://reactome.org/AnalysisService/identifiers/"

APP_NAME = "literature-agent"
APP_VERSION = "0.1.0"

REQUEST_TIMEOUT_SECONDS = 60  # analysis of a large pathway DB can be slow
MAX_RESULTS_LIMIT = 10


class ReactomeError(RuntimeError):
    """Reactome lookup failed: bad input, network/HTTP, or a malformed
    response. "No pathways found" is NOT an error — it returns []."""


def get_reactome_pathways(
    gene: str,
    species: str,
    max_results: int = 5,
) -> list[ReactomePathway]:
    """Map a gene symbol to Reactome pathways.

    Args:
        gene: gene symbol, e.g. "TP53".
        species: species name for the analysis filter, e.g. "Homo sapiens".
        max_results: 1..10 pathways returned (default 5).

    Returns:
        Pathways in API analysis-result order, de-duplicated by
        stable ID. Empty list when the identifier maps to nothing.

    Raises:
        ValueError: blank gene/species or ``max_results`` out of range.
        ReactomeError: network/HTTP failure or malformed response.
    """
    if not isinstance(gene, str) or not gene.strip():
        raise ValueError(f"gene must be a non-empty string, got {gene!r}")
    if not isinstance(species, str) or not species.strip():
        raise ValueError(f"species must be a non-empty string, got {species!r}")
    if (
        isinstance(max_results, bool)
        or not isinstance(max_results, int)
        or not 1 <= max_results <= MAX_RESULTS_LIMIT
    ):
        raise ValueError(
            f"max_results must be an integer in [1, {MAX_RESULTS_LIMIT}], "
            f"got {max_results!r}"
        )
    gene = gene.strip()
    species = species.strip()

    response = _post_identifiers(gene, species, max_results)
    pathways = _parse_pathways(response)
    return pathways[:max_results]


# ---------------------------------------------------------------------------
# HTTP + parsing
# ---------------------------------------------------------------------------

def _post_identifiers(gene: str, species: str, max_results: int) -> dict:
    try:
        response = requests.post(
            ANALYSIS_URL,
            params={
                "species": species,
                "pageSize": str(max_results),
                "page": "1",
                "interactors": "false",
            },
            data=gene,
            headers={
                "Content-Type": "text/plain",
                "User-Agent": f"{APP_NAME}/{APP_VERSION}",
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise ReactomeError(
            f"Network error calling {ANALYSIS_URL}: {exc}"
        ) from exc
    if response.status_code != 200:
        raise ReactomeError(
            f"HTTP {response.status_code} from {ANALYSIS_URL}: "
            f"{response.text[:200]}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise ReactomeError(
            f"Reactome returned malformed JSON: {response.text[:200]!r}"
        ) from exc
    if not isinstance(payload, dict):
        raise ReactomeError("Reactome response was not a JSON object")
    return payload


def _parse_pathways(payload: dict) -> list[ReactomePathway]:
    """De-duplicate by stable ID (keep first) in API order."""
    records = payload.get("pathways")
    if records is None:
        return []
    if not isinstance(records, list):
        raise ReactomeError(
            "Reactome response field 'pathways' is not a list — malformed response"
        )
    pathways: list[ReactomePathway] = []
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            raise ReactomeError("Reactome pathway record is not an object")
        stable_id = record.get("stId")
        name = record.get("name")
        if not isinstance(stable_id, str) or not stable_id.strip():
            raise ReactomeError(
                "Reactome pathway record is missing 'stId' — malformed response"
            )
        if not isinstance(name, str) or not name.strip():
            raise ReactomeError(
                f"Reactome pathway {stable_id} is missing 'name'"
            )
        if stable_id in seen:
            continue
        seen.add(stable_id)
        species_field = record.get("species")
        species_name = (
            species_field.get("name")
            if isinstance(species_field, dict)
            else None
        )
        pathways.append(
            ReactomePathway(
                stable_id=stable_id,
                name=name,
                species=species_name,
                is_disease=record.get("inDisease"),
                is_inferred=None,  # not provided by this endpoint (verified)
            )
        )
    return pathways
