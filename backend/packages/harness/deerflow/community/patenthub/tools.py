"""
PatentHub patent search tools.

All tools share the same api_token and base_url configured under the `patent_search`
tool entry in config.yaml:

    tools:
      - name: patent_search
        group: web
        use: deerflow.community.patenthub.tools:patent_search_tool
        api_token: $PATENTHUB_API_TOKEN
        base_url: https://www.patenthub.cn  # optional, defaults to this value

IMPORTANT API CONSTRAINT:
  Patent IDs returned by `patent_search` are session-bound and expire after 60 minutes.
  The `patent_get_detail`, `patent_get_legal_history`, `patent_get_citations` tools
  MUST receive an ID that was obtained from a recent `patent_search` call.
  Directly constructed IDs will be rejected with error code 215.

Available endpoints (varies by subscription):
  /api/s             - patent_search_tool
  /api/patent/base   - combined into patent_get_detail_tool
  /api/patent/claims - combined into patent_get_detail_tool
  /api/patent/desc   - combined into patent_get_detail_tool
  /api/patent/tx     - patent_get_legal_history_tool
  /api/patent/citing - patent_get_citations_tool
  /api/a/portrait    - enterprise_patent_portrait_tool
"""

import json
from typing import Literal

import httpx
from langchain.tools import tool

from deerflow.config import get_app_config

_BASE_URL = "https://www.patenthub.cn"
_API_VERSION = "1"


def _get_patenthub_config() -> tuple[str, str]:
    """Return (api_token, base_url) from the patent_search tool config."""
    config = get_app_config().get_tool_config("patent_search")
    if config is None:
        raise ValueError(
            "patenthub tools require a 'patent_search' entry in config.yaml "
            "with api_token set."
        )
    api_token: str = config.model_extra.get("api_token", "")
    base_url: str = config.model_extra.get("base_url", _BASE_URL).rstrip("/")
    return api_token, base_url


def _get(path: str, params: dict) -> dict:
    """Execute a GET request against the PatentHub API."""
    api_token, base_url = _get_patenthub_config()
    clean_params = {k: v for k, v in params.items() if v is not None}
    clean_params["t"] = api_token
    clean_params["v"] = _API_VERSION
    try:
        resp = httpx.get(f"{base_url}{path}", params=clean_params, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as e:
        return {"code": e.response.status_code, "success": False, "error": str(e)}
    except Exception as e:
        return {"code": 500, "success": False, "error": str(e)}


def _format_json(data: dict | list) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 1. Patent Search
# ---------------------------------------------------------------------------


@tool("patent_search", parse_docstring=True)
def patent_search_tool(
    q: str,
    ds: Literal["cn", "all"] = "cn",
    page: int = 1,
    page_size: int = 10,
    sort: str = "relation",
    highlight: bool = False,
) -> str:
    """Search patents using keywords or structured query syntax.

    Returns a paginated list of matching patents. The `id` field in each result
    is required by patent_get_detail, patent_get_legal_history,
    patent_get_citations, and patent_get_similar — these IDs expire 60 minutes
    after this search call.

    Query syntax examples:
    - Simple keyword: q="石墨烯"
    - Field-scoped: q="agency:北京市柳沈律师事务所"
    - Date range: q="documentYear:[2014 TO 2015] AND 石墨烯"
    - Combined: q="石墨烯 AND type:发明授权 AND legalStatus:有效专利"

    Supported sort values: relation, applicationDate, !applicationDate,
    documentDate, !documentDate, rank (! prefix = descending).

    Max 1000 records total (100 pages × 10, or 20 pages × 50).
    For broader coverage split the query by time range.

    Args:
        q: Search query — keyword or structured query expression.
        ds: Data scope: "cn" for China only, "all" for global. Default "cn".
        page: Page number (1–100). Default 1.
        page_size: Results per page (1–50). Default 10.
        sort: Sort field. Default "relation".
        highlight: Whether to highlight matched terms in results. Default False.
    """
    data = _get(
        "/api/s",
        {
            "q": q,
            "ds": ds,
            "p": page,
            "ps": page_size,
            "s": sort,
            "hl": 1 if highlight else 0,
        },
    )
    if not data.get("success"):
        return _format_json({"error": data.get("error", "Request failed"), "code": data.get("code")})

    patents = data.get("patents", [])
    result = {
        "total": data.get("total"),
        "page": page,
        "total_pages": data.get("totalPages"),
        "next_page": data.get("nextPage"),
        "took_ms": data.get("took"),
        "patents": [
            {
                "id": p.get("id"),
                "title": p.get("title"),
                "applicant": p.get("applicant"),
                "application_date": p.get("applicationDate"),
                "application_number": p.get("applicationNumber"),
                "document_number": p.get("documentNumber"),
                "document_date": p.get("documentDate"),
                "type": p.get("type"),
                "legal_status": p.get("legalStatus"),
                "main_ipc": p.get("mainIpc"),
                "rank": p.get("rank"),
                "inventor": p.get("inventor"),
                "summary": p.get("summary", "")[:300] + ("..." if len(p.get("summary", "")) > 300 else ""),
            }
            for p in patents
        ],
    }
    return _format_json(result)


# ---------------------------------------------------------------------------
# 2. Patent Detail (merges /api/patent/base + /api/patent/claims + /api/patent/desc)
# ---------------------------------------------------------------------------


@tool("patent_get_detail", parse_docstring=True)
def patent_get_detail_tool(patent_id: str, include_description: bool = False) -> str:
    """Retrieve comprehensive details for a single patent: basic bibliographic
    info, full claims text, and optionally the complete description.

    Makes up to 3 API calls internally (base + claims + description) and
    merges them into one result.

    IMPORTANT: `patent_id` MUST be an `id` value returned by a recent
    `patent_search` call (valid for 60 minutes). Arbitrary IDs will be
    rejected with error code 215.

    Args:
        patent_id: The unique patent ID obtained from patent_search results.
        include_description: Whether to include the full description text.
            Descriptions can be very long — set True only when needed.
            Default False.
    """
    base_data = _get("/api/patent/base", {"id": patent_id})
    if not base_data.get("success"):
        return _format_json({"error": base_data.get("error", "Request failed"), "code": base_data.get("code")})

    claims_data = _get("/api/patent/claims", {"id": patent_id})
    p = base_data.get("patent", {})

    result = {
        "id": p.get("id"),
        "title": p.get("title"),
        "type": p.get("type"),
        "legal_status": p.get("legalStatus"),
        "current_status": p.get("currentStatus"),
        "application_number": p.get("applicationNumber"),
        "application_date": p.get("applicationDate"),
        "document_number": p.get("documentNumber"),
        "document_date": p.get("documentDate"),
        "applicant": p.get("applicant"),
        "first_applicant": p.get("firstApplicant"),
        "current_assignee": p.get("currentAssignee"),
        "assignee": p.get("assignee"),
        "inventor": p.get("inventor"),
        "first_inventor": p.get("firstInventor"),
        "applicant_address": p.get("applicantAddress"),
        "agency": p.get("agency"),
        "agent": p.get("agent"),
        "ipc": p.get("ipc"),
        "main_ipc": p.get("mainIpc"),
        "loc": p.get("loc"),
        "priority_number": p.get("priorityNumber"),
        "full_priority_number": p.get("fullPriorityNumber"),
        "pct_date": p.get("pctDate"),
        "pct_application_data": p.get("pctApplicationData"),
        "pct_publication_data": p.get("pctPublicationData"),
        "image_path": p.get("imagePath"),
        "pdf_list": p.get("pdfList", []),
        "summary": p.get("summary"),
        "claims": claims_data.get("patent", {}).get("claims") if claims_data.get("success") else None,
    }

    if include_description:
        desc_data = _get("/api/patent/desc", {"id": patent_id})
        result["description"] = desc_data.get("patent", {}).get("description") if desc_data.get("success") else None

    return _format_json(result)


# ---------------------------------------------------------------------------
# 3. Patent Legal History
# ---------------------------------------------------------------------------


@tool("patent_get_legal_history", parse_docstring=True)
def patent_get_legal_history_tool(patent_id: str) -> str:
    """Retrieve the full legal event history (transactions) for a single patent,
    including publication, examination, grant, assignment transfers, and
    expiration events.

    IMPORTANT: `patent_id` MUST be an `id` value returned by a recent
    `patent_search` call (valid for 60 minutes).

    Args:
        patent_id: The unique patent ID obtained from patent_search results.
    """
    data = _get("/api/patent/tx", {"id": patent_id})
    if not data.get("success"):
        return _format_json({"error": data.get("error", "Request failed"), "code": data.get("code")})

    transactions = data.get("transactions", [])
    result = {
        "patent_id": patent_id,
        "transaction_count": len(transactions),
        "transactions": [
            {
                "date": t.get("date"),
                "type": t.get("type"),
                "application_number": t.get("applicationNumber"),
                "content": t.get("content"),
            }
            for t in transactions
        ],
    }
    return _format_json(result)


# ---------------------------------------------------------------------------
# 4. Patent Citations
# ---------------------------------------------------------------------------


@tool("patent_get_citations", parse_docstring=True)
def patent_get_citations_tool(patent_id: str) -> str:
    """Retrieve citation data for a single patent: patents it cites (forward
    references), patents it is cited by (backward references), and non-patent
    literature references.

    IMPORTANT: `patent_id` MUST be an `id` value returned by a recent
    `patent_search` call (valid for 60 minutes).

    Args:
        patent_id: The unique patent ID obtained from patent_search results.
    """
    data = _get("/api/patent/citing", {"id": patent_id})
    if not data.get("success"):
        return _format_json({"error": data.get("error", "Request failed"), "code": data.get("code")})

    def _summarize_patent(p: dict) -> dict:
        return {
            "id": p.get("id"),
            "title": p.get("title"),
            "applicant": p.get("applicant"),
            "application_date": p.get("applicationDate"),
            "application_number": p.get("applicationNumber"),
            "type": p.get("type"),
            "legal_status": p.get("legalStatus"),
            "main_ipc": p.get("mainIpc"),
        }

    result = {
        "patent_id": patent_id,
        "cited_by": [_summarize_patent(p) for p in data.get("citedList", [])],
        "patent_references": [_summarize_patent(p) for p in data.get("patentXref", [])],
        "non_patent_references": data.get("noPatentXref", []),
    }
    return _format_json(result)


# ---------------------------------------------------------------------------
# 5. Enterprise Patent Portrait
# ---------------------------------------------------------------------------


@tool("enterprise_patent_portrait", parse_docstring=True)
def enterprise_patent_portrait_tool(enterprise_name: str) -> str:
    """Retrieve a comprehensive patent portfolio analysis for an enterprise,
    including legal status distribution, patent type breakdown, filing/
    publication/grant trends by year, technology (IPC) distribution, top
    inventors, and main competitors.

    Use this for due diligence, IP landscape analysis, or competitive
    intelligence on a specific company.

    NOTE: The enterprise_name must be the full official name of the company
    for accurate results (e.g. "华为技术有限公司" not "华为").

    Args:
        enterprise_name: Full official name of the enterprise to analyze.
    """
    data = _get("/api/a/portrait", {"en": enterprise_name})
    if not data.get("success"):
        return _format_json({"error": data.get("error", "Request failed"), "code": data.get("code")})

    portrait = data.get("enterprisePortrait", {})
    result = {
        "enterprise": enterprise_name,
        "legal_status_distribution": portrait.get("legalMap", {}),
        "patent_type_distribution": portrait.get("typeMap", {}),
        "geographic_distribution": portrait.get("areaMap", {}),
        "ipc_distribution": portrait.get("ipcMap", {}),
        "top_competitors": portrait.get("compareMap", {}),
        "top_inventors": portrait.get("inventorMap", {}),
        "application_trend_by_year": portrait.get("applicationYearMap", {}),
        "publication_trend_by_year": portrait.get("publicationYearMap", {}),
        "grant_trend_by_year": portrait.get("grantYearMap", {}),
        "legal_status_by_year": portrait.get("legalApplicationYearMap", {}),
    }
    return _format_json(result)


