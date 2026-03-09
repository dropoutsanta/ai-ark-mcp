"""AI Ark MCP Server.

Exposes the full AI Ark API (company search, people search, reverse lookup,
phone finder, personality analysis, email export, and more) as MCP tools.

Supports OAuth 2.1 credential input — Authenticate with your AI Ark API key
and you're in.  Each MCP connection gets its own credentials.
"""

from __future__ import annotations

import contextvars
import hashlib
import base64
import html as html_module
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any, Optional

import httpx
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, HTMLResponse, RedirectResponse
from starlette.routing import Route, Mount
from starlette.types import ASGIApp, Receive, Scope, Send

# ── Per-request credentials ───────────────────────────────────────────────────

_ctx_api_key = contextvars.ContextVar("ark_api_key", default="")

_ARK_BASE = "https://api.ai-ark.com/api/developer-portal"

# ── OAuth 2.1 stores (persisted to disk) ──────────────────────────────────────

_registered_clients: dict[str, dict] = {}
_auth_codes: dict[str, dict] = {}
_token_credentials: dict[str, dict] = {}

_MCP_BASE_URL = os.environ.get("MCP_BASE_URL", "https://ai-ark-mcp.fly.dev")
_STATE_PATH = Path(os.environ.get("OAUTH_STATE_PATH", "/data/oauth_state.json"))
_RESULTS_DIR = Path(os.environ.get("RESULTS_DIR", "/data/results"))


def _save_state() -> None:
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STATE_PATH.with_suffix(".tmp")
        data = {
            "registered_clients": _registered_clients,
            "token_credentials": _token_credentials,
        }
        tmp.write_text(json.dumps(data))
        tmp.rename(_STATE_PATH)
    except Exception as exc:
        print(f"[persist] save failed: {exc}", flush=True)


def _load_state() -> None:
    global _registered_clients, _token_credentials
    try:
        if _STATE_PATH.exists():
            data = json.loads(_STATE_PATH.read_text())
            _registered_clients.update(data.get("registered_clients", {}))
            _token_credentials.update(data.get("token_credentials", {}))
            print(f"[persist] loaded {len(_registered_clients)} clients, {len(_token_credentials)} tokens", flush=True)
        else:
            print(f"[persist] no state file, starting fresh", flush=True)
    except Exception as exc:
        print(f"[persist] load failed: {exc}", flush=True)


_load_state()


def _get_base_url() -> str:
    return _MCP_BASE_URL.rstrip("/")


def _save_receipt_mapping(track_id: str, receipt_id: str) -> None:
    """Map a trackId to a receipt ID so get_export_results can find webhook data."""
    mapping_path = _RESULTS_DIR / "_mappings.json"
    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    mappings = {}
    if mapping_path.exists():
        try:
            mappings = json.loads(mapping_path.read_text())
        except Exception:
            pass
    mappings[track_id] = receipt_id
    mapping_path.write_text(json.dumps(mappings))


def _load_receipt_id(track_id: str) -> str | None:
    """Look up the receipt ID for a given trackId."""
    mapping_path = _RESULTS_DIR / "_mappings.json"
    if not mapping_path.exists():
        return None
    try:
        mappings = json.loads(mapping_path.read_text())
        return mappings.get(track_id)
    except Exception:
        return None


# ── Ark API client ────────────────────────────────────────────────────────────

def _ark_request(method: str, path: str, *, body: dict | None = None, timeout: float = 30.0) -> dict:
    api_key = _ctx_api_key.get()
    if not api_key:
        return {"error": "Not authenticated. Please re-authenticate with your AI Ark API key."}

    url = f"{_ARK_BASE}{path}"
    headers = {
        "X-TOKEN": api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    kwargs: dict[str, Any] = {"method": method, "url": url, "headers": headers, "timeout": timeout}
    if body is not None:
        kwargs["json"] = body

    try:
        resp = httpx.request(**kwargs)
    except httpx.TimeoutException:
        return {"error": f"Request timed out after {timeout}s"}
    except httpx.RequestError as exc:
        return {"error": f"Request failed: {exc}"}

    if resp.status_code >= 400:
        try:
            err = resp.json()
        except Exception:
            err = resp.text
        return {"error": f"HTTP {resp.status_code}", "details": err}

    text = resp.text
    if not text:
        return {"success": True}
    try:
        return resp.json()
    except Exception:
        return {"data": text}


# ── FastMCP instance ──────────────────────────────────────────────────────────

mcp = FastMCP("ai-ark", host="0.0.0.0", port=int(os.environ.get("MCP_PORT", "8000")))


# ── Helper: build nested filter from flat params ─────────────────────────────

def _build_any_include(values: list[str]) -> dict:
    return {"any": {"include": values}}


def _build_any_include_smart(values: list[str]) -> dict:
    return {"any": {"include": {"mode": "SMART", "content": values}}}


def _parse_json_or_csv(value: str) -> list[str]:
    """Parse a JSON array string or comma-separated string into a list."""
    value = value.strip()
    if value.startswith("["):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(v) for v in parsed]
        except json.JSONDecodeError:
            pass
    return [v.strip() for v in value.split(",") if v.strip()]


def _coerce_filters(raw) -> dict | None:
    """Accept filters_json as a JSON string or an already-parsed dict."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        return json.loads(raw)
    return None


def _parse_range_pairs(value: str) -> list[dict]:
    """Parse range specs like '1-10,51-200' into [{start:1,end:10},{start:51,end:200}]."""
    ranges = []
    for part in value.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-", 1)
            try:
                ranges.append({"start": int(lo.strip()), "end": int(hi.strip())})
            except ValueError:
                pass
    return ranges


# ── Tools ─────────────────────────────────────────────────────────────────────

@mcp.tool()
def search_companies(
    filters_json: Any = None,
    industries: Optional[str] = None,
    locations: Optional[str] = None,
    employee_size: Optional[str] = None,
    company_names: Optional[str] = None,
    domains: Optional[str] = None,
    technologies: Optional[str] = None,
    company_types: Optional[str] = None,
    founded_year_start: Optional[int] = None,
    founded_year_end: Optional[int] = None,
    revenue_start: Optional[int] = None,
    revenue_end: Optional[int] = None,
    keywords: Optional[str] = None,
    lookalike_domains: Optional[str] = None,
    page: int = 0,
    size: int = 25,
) -> dict:
    """Search 69M+ enriched company profiles in the AI Ark database.

    You can either pass the full nested API body as `filters_json`, or use
    the convenient flat parameters below (which get assembled automatically).

    Args:
        filters_json: Full AI Ark request body as JSON string. If provided,
            all other filter params are ignored. Must include "account" key
            with nested filters. See docs.ai-ark.com/reference for schema.
        industries: Comma-separated industries (e.g. "technology,retail").
        locations: Comma-separated HQ locations (e.g. "United States,Germany").
        employee_size: Comma-separated ranges (e.g. "51-200,201-500").
        company_names: Comma-separated company names to search for.
        domains: Comma-separated domains (e.g. "apple.com,google.com").
        technologies: Comma-separated technologies (e.g. "React,AWS").
        company_types: Comma-separated types. Valid: SELF_EMPLOYED, SOLE_PROPRIETORSHIP,
            PARTNERSHIP, PRIVATELY_HELD, PUBLIC_COMPANY, GOVERNMENT_AGENCY,
            NON_PROFIT, SELF_OWNED, EDUCATIONAL.
        founded_year_start: Minimum founded year (e.g. 2015).
        founded_year_end: Maximum founded year (e.g. 2023).
        revenue_start: Minimum annual revenue in USD.
        revenue_end: Maximum annual revenue in USD.
        keywords: Comma-separated keywords to match against company name,
            description, SEO, and keyword fields.
        lookalike_domains: Up to 5 company domains/LinkedIn URLs to find
            similar companies (e.g. "amazon.com,shopify.com").
        page: Page number (0-based). Default 0.
        size: Results per page (max 100). Default 25.
    """
    if filters_json:
        try:
            body = _coerce_filters(filters_json)
        except (json.JSONDecodeError, TypeError):
            return {"error": "Invalid JSON in filters_json"}
        body.setdefault("page", page)
        body.setdefault("size", min(size, 100))
        return _ark_request("POST", "/v1/companies", body=body)

    account: dict[str, Any] = {}

    if industries:
        account["industry"] = _build_any_include([i.lower() for i in _parse_json_or_csv(industries)])
    if locations:
        account["location"] = _build_any_include([loc.lower() for loc in _parse_json_or_csv(locations)])
    if employee_size:
        ranges = _parse_range_pairs(employee_size)
        if ranges:
            account["employeeSize"] = {"type": "RANGE", "range": ranges}
    if company_names:
        account["name"] = _build_any_include_smart(_parse_json_or_csv(company_names))
    if domains:
        account["domain"] = _build_any_include(_parse_json_or_csv(domains))
    if technologies:
        account["technology"] = _build_any_include(_parse_json_or_csv(technologies))
    if company_types:
        account["type"] = _build_any_include(_parse_json_or_csv(company_types))
    if founded_year_start is not None or founded_year_end is not None:
        r: dict[str, int] = {}
        if founded_year_start is not None:
            r["start"] = founded_year_start
        if founded_year_end is not None:
            r["end"] = founded_year_end
        account["foundedYear"] = {"type": "RANGE", "range": r}
    if revenue_start is not None or revenue_end is not None:
        rr: dict[str, int] = {}
        if revenue_start is not None:
            rr["start"] = revenue_start
        if revenue_end is not None:
            rr["end"] = revenue_end
        account["revenue"] = {"type": "RANGE", "range": [rr]}
    if keywords:
        kw_list = _parse_json_or_csv(keywords)
        account["keyword"] = {
            "any": {
                "include": {
                    "sources": [
                        {"mode": "SMART", "source": "NAME"},
                        {"mode": "SMART", "source": "KEYWORD"},
                        {"mode": "SMART", "source": "SEO"},
                        {"mode": "SMART", "source": "DESCRIPTION"},
                    ],
                    "content": kw_list,
                }
            }
        }

    body: dict[str, Any] = {"page": page, "size": min(size, 100)}
    if account:
        body["account"] = account
    if lookalike_domains:
        body["lookalikeDomains"] = _parse_json_or_csv(lookalike_domains)[:5]

    return _ark_request("POST", "/v1/companies", body=body)


@mcp.tool()
def search_people(
    filters_json: Any = None,
    job_titles: Optional[str] = None,
    locations: Optional[str] = None,
    seniority_levels: Optional[str] = None,
    departments: Optional[str] = None,
    skills: Optional[str] = None,
    languages: Optional[str] = None,
    profile_keywords: Optional[str] = None,
    linkedin_urls: Optional[str] = None,
    industries: Optional[str] = None,
    company_hq_locations: Optional[str] = None,
    employee_size: Optional[str] = None,
    company_types: Optional[str] = None,
    technologies: Optional[str] = None,
    company_keywords: Optional[str] = None,
    founded_year_start: Optional[int] = None,
    founded_year_end: Optional[int] = None,
    revenue_start: Optional[int] = None,
    revenue_end: Optional[int] = None,
    page: int = 0,
    size: int = 25,
) -> dict:
    """Search 400M+ enriched people profiles in the AI Ark database.

    Returns full profiles with name, title, company, location, LinkedIn,
    skills, education, work history, and more. The response includes a
    `trackId` that can be used with `find_emails_by_track_id` to find
    verified email addresses for the results.

    You can either pass the full nested API body as `filters_json`, or use
    the flat parameters (which get assembled automatically).

    Args:
        filters_json: Full AI Ark request body as JSON string. If provided,
            all other filter params are ignored.
        job_titles: Comma-separated titles (e.g. "CEO,CTO,VP of Sales").
            Uses SMART matching so "Sales Director" also matches
            "Director of Sales".
        locations: Comma-separated person locations (e.g. "new york,california").
        seniority_levels: Comma-separated levels (e.g. "c_suite,vp,director").
            Valid: owner, founder, c_suite, partner, vp, director, manager,
            senior, entry, intern, unpaid.
        departments: Comma-separated departments (e.g. "sales,marketing,engineering").
        skills: Comma-separated skills (e.g. "Python,Machine Learning").
        languages: Comma-separated languages (e.g. "English,French").
        profile_keywords: Comma-separated keywords to match in headline,
            summary, skills, and work history.
        linkedin_urls: Comma-separated LinkedIn profile URLs to look up
            specific people (e.g. "https://linkedin.com/in/johndoe").
        industries: Comma-separated company industries (e.g. "technology,saas").
        company_hq_locations: Comma-separated company HQ locations.
        employee_size: Comma-separated employee ranges (e.g. "51-200,201-500").
        company_types: Comma-separated company types (e.g. "PRIVATELY_HELD").
        technologies: Comma-separated tech stack (e.g. "Salesforce,HubSpot").
        company_keywords: Comma-separated keywords for company name/description.
        founded_year_start: Min company founded year.
        founded_year_end: Max company founded year.
        revenue_start: Min annual revenue in USD.
        revenue_end: Max annual revenue in USD.
        page: Page number (0-based). Default 0.
        size: Results per page (max 100). Default 25.
    """
    if filters_json:
        try:
            body = _coerce_filters(filters_json)
        except (json.JSONDecodeError, TypeError):
            return {"error": "Invalid JSON in filters_json"}
        body.setdefault("page", page)
        body.setdefault("size", min(size, 100))
        return _ark_request("POST", "/v1/people", body=body)

    contact: dict[str, Any] = {}
    account: dict[str, Any] = {}

    if job_titles:
        titles = _parse_json_or_csv(job_titles)
        contact["experience"] = {
            "current": {"title": _build_any_include_smart(titles)}
        }
    if locations:
        contact["location"] = _build_any_include([loc.lower() for loc in _parse_json_or_csv(locations)])
    if seniority_levels:
        contact["seniority"] = _build_any_include(_parse_json_or_csv(seniority_levels))
    if departments:
        contact["departmentAndFunction"] = _build_any_include(_parse_json_or_csv(departments))
    if skills:
        contact["skill"] = _build_any_include_smart(_parse_json_or_csv(skills))
    if languages:
        contact["language"] = _build_any_include_smart(_parse_json_or_csv(languages))
    if profile_keywords:
        kw = _parse_json_or_csv(profile_keywords)
        contact["keyword"] = {
            "any": {
                "include": {
                    "sources": [
                        {"mode": "SMART", "source": "HEADLINE"},
                        {"mode": "SMART", "source": "SUMMARY"},
                        {"mode": "SMART", "source": "SKILL"},
                        {"mode": "SMART", "source": "WORK_HISTORY_DESCRIPTION"},
                    ],
                    "content": kw,
                }
            }
        }
    if linkedin_urls:
        contact["linkedin"] = _build_any_include(_parse_json_or_csv(linkedin_urls))

    if industries:
        account["industry"] = _build_any_include([i.lower() for i in _parse_json_or_csv(industries)])
    if company_hq_locations:
        account["location"] = _build_any_include([loc.lower() for loc in _parse_json_or_csv(company_hq_locations)])
    if employee_size:
        ranges = _parse_range_pairs(employee_size)
        if ranges:
            account["employeeSize"] = {"type": "RANGE", "range": ranges}
    if company_types:
        account["type"] = _build_any_include(_parse_json_or_csv(company_types))
    if technologies:
        account["technology"] = _build_any_include(_parse_json_or_csv(technologies))
    if company_keywords:
        kw2 = _parse_json_or_csv(company_keywords)
        account["keyword"] = {
            "any": {
                "include": {
                    "sources": [
                        {"mode": "SMART", "source": "NAME"},
                        {"mode": "SMART", "source": "KEYWORD"},
                        {"mode": "SMART", "source": "SEO"},
                        {"mode": "SMART", "source": "DESCRIPTION"},
                    ],
                    "content": kw2,
                }
            }
        }
    if founded_year_start is not None or founded_year_end is not None:
        r: dict[str, int] = {}
        if founded_year_start is not None:
            r["start"] = founded_year_start
        if founded_year_end is not None:
            r["end"] = founded_year_end
        account["foundedYear"] = {"type": "RANGE", "range": r}
    if revenue_start is not None or revenue_end is not None:
        rr: dict[str, int] = {}
        if revenue_start is not None:
            rr["start"] = revenue_start
        if revenue_end is not None:
            rr["end"] = revenue_end
        account["revenue"] = {"type": "RANGE", "range": [rr]}

    body: dict[str, Any] = {"page": page, "size": min(size, 100)}
    if contact:
        body["contact"] = contact
    if account:
        body["account"] = account

    return _ark_request("POST", "/v1/people", body=body)


@mcp.tool()
def export_people_with_email(
    filters_json: Any = None,
    job_titles: Optional[str] = None,
    locations: Optional[str] = None,
    seniority_levels: Optional[str] = None,
    departments: Optional[str] = None,
    industries: Optional[str] = None,
    employee_size: Optional[str] = None,
    page: int = 0,
    size: int = 25,
) -> dict:
    """Export people with verified email addresses.

    Uses the same filters as search_people. Email verification happens
    asynchronously. Returns a trackId — use get_export_results(trackId)
    to poll for the full contact + email data once ready.

    Max 10,000 results per export. All emails are verified in real time
    by BounceBan. Typical completion: 15-120 seconds depending on batch size.

    Args:
        filters_json: Full AI Ark request body as JSON string.
        job_titles: Comma-separated job titles.
        locations: Comma-separated person locations.
        seniority_levels: Comma-separated seniority levels.
        departments: Comma-separated departments.
        industries: Comma-separated company industries.
        employee_size: Comma-separated ranges (e.g. "51-200,201-500").
        page: Page number (0-based).
        size: Number of results (max 10000).
    """
    if filters_json:
        try:
            body = _coerce_filters(filters_json)
        except (json.JSONDecodeError, TypeError):
            return {"error": "Invalid JSON in filters_json"}
        body.setdefault("page", page)
        body.setdefault("size", min(size, 10000))
    else:
        contact: dict[str, Any] = {}
        account: dict[str, Any] = {}

        if job_titles:
            titles = _parse_json_or_csv(job_titles)
            contact["experience"] = {
                "current": {"title": _build_any_include_smart(titles)}
            }
        if locations:
            contact["location"] = _build_any_include([loc.lower() for loc in _parse_json_or_csv(locations)])
        if seniority_levels:
            contact["seniority"] = _build_any_include(_parse_json_or_csv(seniority_levels))
        if departments:
            contact["departmentAndFunction"] = _build_any_include(_parse_json_or_csv(departments))
        if industries:
            account["industry"] = _build_any_include([i.lower() for i in _parse_json_or_csv(industries)])
        if employee_size:
            ranges = _parse_range_pairs(employee_size)
            if ranges:
                account["employeeSize"] = {"type": "RANGE", "range": ranges}

        body: dict[str, Any] = {"page": page, "size": min(size, 10000)}
        if contact:
            body["contact"] = contact
        if account:
            body["account"] = account

    receipt_id = secrets.token_urlsafe(16)
    body["webhook"] = _webhook_url_for(receipt_id)

    result = _ark_request("POST", "/v1/people/export", body=body)

    track_id = result.get("trackId")
    if track_id and "error" not in result:
        _save_receipt_mapping(track_id, receipt_id)
        result["_hint"] = f"Use get_export_results(track_id='{track_id}') to poll for results."

    return result


@mcp.tool()
def reverse_people_lookup(search: str) -> dict:
    """Look up a person by email address or phone number.

    Returns the full profile (name, title, company, location, LinkedIn,
    work history, education, skills) for the matching person.

    Args:
        search: Email address or phone number to look up
            (e.g. "john@example.com" or "+14155551234").
    """
    return _ark_request("POST", "/v1/people/reverse-lookup", body={"search": search})


@mcp.tool()
def find_mobile_phone(
    linkedin: Optional[str] = None,
    domain: Optional[str] = None,
    name: Optional[str] = None,
) -> dict:
    """Find mobile phone numbers for a person.

    Two search modes:
    - LinkedIn: Provide only `linkedin` URL.
    - Domain + Name: Provide both `domain` and `name`.

    Args:
        linkedin: LinkedIn profile URL (e.g. "https://www.linkedin.com/in/johndoe").
        domain: Company domain (e.g. "acme.com"). Must be used with `name`.
        name: Person's full name (e.g. "John Doe"). Must be used with `domain`.
    """
    body: dict[str, Any] = {}
    if linkedin:
        body["linkedin"] = linkedin
    if domain:
        body["domain"] = domain
    if name:
        body["name"] = name
    return _ark_request("POST", "/v1/people/mobile-phone-finder", body=body)


@mcp.tool()
def analyze_personality(url: str) -> dict:
    """Analyze a person's personality based on their LinkedIn profile.

    Returns DISC assessment, OCEAN (Big Five) scores, selling tips
    (email style, tone, closing line, subject line advice), communication
    preferences, and key decision-making traits.

    Useful for personalizing outreach emails and understanding how to
    approach a prospect.

    Args:
        url: LinkedIn profile URL (e.g. "https://www.linkedin.com/in/johndoe").
    """
    return _ark_request("POST", "/v1/people/analysis", body={"url": url})


@mcp.tool()
def find_emails_by_track_id(track_id: str) -> dict:
    """Trigger email finding for a people search result using its trackId.

    The trackId comes from search_people responses. Each trackId can only
    be used ONCE and expires 6 hours after the original search.

    All emails are verified in real time by BounceBan. Use
    get_export_results(track_id) to poll for the full results once
    processing is complete. Typical completion: 15-120 seconds.

    Args:
        track_id: The trackId from a search_people response.
    """
    receipt_id = secrets.token_urlsafe(16)
    body: dict[str, Any] = {
        "trackId": track_id,
        "webhook": _webhook_url_for(receipt_id),
    }
    result = _ark_request("POST", "/v1/people/email-finder", body=body)

    if "error" not in result:
        result_track = result.get("trackId", track_id)
        _save_receipt_mapping(result_track, receipt_id)
        result["_hint"] = f"Use get_export_results(track_id='{result_track}') to poll for results."

    return result


@mcp.tool()
def get_email_statistics(track_id: str) -> dict:
    """Check email-finding progress for a given trackId.

    Returns statistics (total people, emails found so far) and state
    (PENDING, COMPLETED, etc.). Use this to poll progress after calling
    find_emails_by_track_id or export_people_with_email.

    Args:
        track_id: The trackId from a search/export/email-finder response.
    """
    return _ark_request("GET", f"/v1/people/statistics/{track_id}")


@mcp.tool()
def get_export_results(track_id: str) -> dict:
    """Retrieve the results of an email export or email-finder job.

    After calling export_people_with_email or find_emails_by_track_id,
    use this tool to poll for results. Returns the full contact + verified
    email data once complete, or a progress update if still processing.

    Typical flow:
      1. Call export_people_with_email → get trackId
      2. Call get_export_results(trackId) → "processing" or full data

    Args:
        track_id: The trackId from an export or email-finder response.
    """
    receipt_id = _load_receipt_id(track_id)
    if receipt_id:
        result_path = _RESULTS_DIR / f"{receipt_id}.json"
        if result_path.exists():
            try:
                return json.loads(result_path.read_text())
            except Exception as exc:
                return {"error": f"Failed to read results: {exc}"}

    stats = _ark_request("GET", f"/v1/people/statistics/{track_id}")
    if "error" in stats:
        return stats

    state = stats.get("state", "UNKNOWN")
    total = stats.get("statistics", {}).get("total", 0)
    found = stats.get("statistics", {}).get("found", 0)

    if state == "DONE":
        if receipt_id:
            result_path = _RESULTS_DIR / f"{receipt_id}.json"
            if result_path.exists():
                return json.loads(result_path.read_text())
        return {
            "status": "completed_awaiting_delivery",
            "message": f"Email finding is DONE ({found}/{total} found). Results arriving shortly — try again in a few seconds.",
            "statistics": stats.get("statistics"),
        }

    return {
        "status": "processing",
        "message": f"Still processing: {found}/{total} emails found so far. State: {state}. Poll again in 10-30 seconds.",
        "statistics": stats.get("statistics"),
        "state": state,
    }


@mcp.tool()
def get_credits() -> dict:
    """Check how many API credits remain in your AI Ark account.

    Returns the total number of remaining credits.
    """
    return _ark_request("GET", "/v1/payments/credits")


# ── Webhook receiver ─────────────────────────────────────────────────────────

async def webhook_receiver(request: Request):
    """Receives webhook POSTs from AI Ark and stores the payload."""
    track_id = request.path_params.get("track_id", "")
    if not track_id:
        return JSONResponse({"error": "missing track_id"}, status_code=400)

    try:
        payload = await request.json()
    except Exception:
        body = await request.body()
        payload = {"raw": body.decode(errors="replace")}

    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    result_path = _RESULTS_DIR / f"{track_id}.json"
    result_path.write_text(json.dumps(payload))
    print(f"[webhook] stored results for trackId={track_id} ({len(json.dumps(payload))} bytes)", flush=True)

    return JSONResponse({"received": True})


def _webhook_url_for(track_id: str) -> str:
    """Generate the MCP server's own webhook URL for a given trackId."""
    return f"{_get_base_url()}/webhook/{track_id}"


# ── OAuth 2.1 endpoints ──────────────────────────────────────────────────────

async def oauth_protected_resource(request: Request):
    base = _get_base_url()
    return JSONResponse({
        "resource": base,
        "authorization_servers": [base],
        "scopes_supported": ["mcp:tools"],
        "bearer_methods_supported": ["header"],
    })


async def oauth_authorization_server(request: Request):
    base = _get_base_url()
    return JSONResponse({
        "issuer": base,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "registration_endpoint": f"{base}/register",
        "scopes_supported": ["mcp:tools"],
        "grant_types_supported": ["authorization_code"],
        "response_types_supported": ["code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
    })


async def register(request: Request):
    body = await request.json()
    client_id = secrets.token_urlsafe(16)
    info = {
        "client_id": client_id,
        "client_name": body.get("client_name", "MCP Client"),
        "redirect_uris": body.get("redirect_uris", []),
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    _registered_clients[client_id] = info
    _save_state()
    print(f"[register] client_id={client_id}", flush=True)
    return JSONResponse(info, status_code=201)


_AUTHORIZE_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Ark MCP - Connect</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:system-ui,-apple-system,sans-serif;background:#f0f2f5;display:flex;
  justify-content:center;align-items:center;min-height:100vh}}
.card{{background:#fff;border-radius:16px;padding:40px;max-width:420px;width:100%;
  box-shadow:0 4px 24px rgba(0,0,0,.08)}}
.logo{{font-size:28px;font-weight:700;margin-bottom:4px;color:#1a1a1a}}
.logo span{{color:#6366f1}}
.subtitle{{color:#666;margin-bottom:28px;font-size:14px;line-height:1.5}}
label{{display:block;font-size:13px;font-weight:600;color:#333;margin-bottom:6px;margin-top:16px}}
input[type=password]{{width:100%;padding:12px 14px;border:1px solid #ddd;
  border-radius:10px;font-size:14px;transition:border-color .2s}}
input:focus{{outline:none;border-color:#6366f1;box-shadow:0 0 0 3px rgba(99,102,241,.15)}}
.hint{{font-size:12px;color:#999;margin-top:4px}}
button{{width:100%;padding:14px;background:#6366f1;color:#fff;border:none;
  border-radius:10px;font-size:15px;font-weight:600;cursor:pointer;margin-top:24px;
  transition:background .2s}}
button:hover{{background:#4f46e5}}
.err{{color:#dc2626;margin-bottom:16px;font-size:14px;padding:10px;
  background:#fef2f2;border-radius:8px}}
</style></head><body>
<div class="card">
<div class="logo">AI Ark <span>MCP</span></div>
<p class="subtitle">Connect your AI Ark account.<br>
Enter your API key to authorize <strong>{client_name}</strong>.</p>
{error_html}
<form method="POST" action="/authorize">
  <input type="hidden" name="client_id" value="{client_id}">
  <input type="hidden" name="redirect_uri" value="{redirect_uri}">
  <input type="hidden" name="state" value="{state}">
  <input type="hidden" name="code_challenge" value="{code_challenge}">
  <input type="hidden" name="code_challenge_method" value="{code_challenge_method}">
  <input type="hidden" name="scope" value="{scope}">
  <input type="hidden" name="response_type" value="{response_type}">
  <label for="api_key">API Key</label>
  <input type="password" id="api_key" name="api_key" placeholder="Your AI Ark API key" required autofocus>
  <div class="hint">Find it at ai-ark.com &rarr; API &rarr; Developer Portal</div>
  <button type="submit">Connect</button>
</form>
</div></body></html>"""


async def authorize(request: Request):
    if request.method == "GET":
        qp = dict(request.query_params)
        client = _registered_clients.get(qp.get("client_id", ""), {})
        return HTMLResponse(_AUTHORIZE_HTML.format(
            client_name=html_module.escape(client.get("client_name", "Unknown")),
            client_id=html_module.escape(qp.get("client_id", "")),
            redirect_uri=html_module.escape(qp.get("redirect_uri", "")),
            state=html_module.escape(qp.get("state", "")),
            code_challenge=html_module.escape(qp.get("code_challenge", "")),
            code_challenge_method=html_module.escape(qp.get("code_challenge_method", "")),
            scope=html_module.escape(qp.get("scope", "")),
            response_type=html_module.escape(qp.get("response_type", "")),
            error_html="",
        ))

    form = await request.form()
    api_key = str(form.get("api_key", "")).strip()

    if not api_key:
        client = _registered_clients.get(str(form.get("client_id", "")), {})
        return HTMLResponse(_AUTHORIZE_HTML.format(
            client_name=html_module.escape(client.get("client_name", "Unknown")),
            client_id=html_module.escape(str(form.get("client_id", ""))),
            redirect_uri=html_module.escape(str(form.get("redirect_uri", ""))),
            state=html_module.escape(str(form.get("state", ""))),
            code_challenge=html_module.escape(str(form.get("code_challenge", ""))),
            code_challenge_method=html_module.escape(str(form.get("code_challenge_method", ""))),
            scope=html_module.escape(str(form.get("scope", ""))),
            response_type=html_module.escape(str(form.get("response_type", ""))),
            error_html='<p class="err">API key is required.</p>',
        ), status_code=200)

    try:
        resp = httpx.get(
            f"{_ARK_BASE}/v1/payments/credits",
            headers={"X-TOKEN": api_key, "Content-Type": "application/json"},
            timeout=10.0,
        )
        if resp.status_code == 401 or resp.status_code == 403:
            raise ValueError("Invalid API key")
    except (httpx.RequestError, ValueError):
        client = _registered_clients.get(str(form.get("client_id", "")), {})
        return HTMLResponse(_AUTHORIZE_HTML.format(
            client_name=html_module.escape(client.get("client_name", "Unknown")),
            client_id=html_module.escape(str(form.get("client_id", ""))),
            redirect_uri=html_module.escape(str(form.get("redirect_uri", ""))),
            state=html_module.escape(str(form.get("state", ""))),
            code_challenge=html_module.escape(str(form.get("code_challenge", ""))),
            code_challenge_method=html_module.escape(str(form.get("code_challenge_method", ""))),
            scope=html_module.escape(str(form.get("scope", ""))),
            response_type=html_module.escape(str(form.get("response_type", ""))),
            error_html='<p class="err">Could not validate your API key. Check it and try again.</p>',
        ), status_code=200)

    code = secrets.token_urlsafe(32)
    _auth_codes[code] = {
        "client_id": str(form.get("client_id", "")),
        "redirect_uri": str(form.get("redirect_uri", "")),
        "code_challenge": str(form.get("code_challenge", "")),
        "code_challenge_method": str(form.get("code_challenge_method", "")),
        "created_at": time.time(),
        "api_key": api_key,
    }

    redirect_uri = str(form.get("redirect_uri", ""))
    state = str(form.get("state", ""))
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{sep}code={code}&state={state}", status_code=302)


async def token_endpoint(request: Request):
    content_type = request.headers.get("content-type", "")
    if "json" in content_type:
        raw = await request.json()
    else:
        form_data = await request.form()
        raw = {k: v for k, v in form_data.items()}

    grant_type = raw.get("grant_type", "")
    code = str(raw.get("code", ""))
    code_verifier = str(raw.get("code_verifier", ""))

    if grant_type != "authorization_code":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    if code not in _auth_codes:
        return JSONResponse({"error": "invalid_grant", "error_description": "Code not found"}, status_code=400)

    code_data = _auth_codes.pop(code)

    if time.time() - code_data["created_at"] > 600:
        return JSONResponse({"error": "invalid_grant", "error_description": "Code expired"}, status_code=400)

    challenge = code_data.get("code_challenge", "")
    if challenge and code_verifier:
        digest = hashlib.sha256(code_verifier.encode()).digest()
        expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        if expected != challenge:
            print(f"[token] PKCE mismatch — accepting anyway", flush=True)

    access_token = secrets.token_urlsafe(48)
    _token_credentials[access_token] = {"api_key": code_data["api_key"]}
    _save_state()
    print(f"[token] SUCCESS: token issued, total={len(_token_credentials)}", flush=True)

    return JSONResponse({
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": 86400 * 365,
        "scope": "mcp:tools",
    })


# ── Auth middleware ───────────────────────────────────────────────────────────

_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type, Accept, Mcp-Session-Id",
    "Access-Control-Expose-Headers": "WWW-Authenticate, Mcp-Session-Id",
    "Access-Control-Max-Age": "86400",
}


class ArkAuthMiddleware:
    _PUBLIC_PREFIXES = (
        "/.well-known/",
        "/authorize",
        "/token",
        "/register",
        "/webhook/",
    )

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET")
        if method == "OPTIONS":
            response = JSONResponse({}, status_code=204, headers=_CORS_HEADERS)
            await response(scope, receive, send)
            return

        path = scope.get("path", "")
        if any(path.startswith(pfx) for pfx in self._PUBLIC_PREFIXES):
            await self._with_cors(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        auth = headers.get(b"authorization", b"").decode()
        bearer = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""

        if not bearer:
            query = scope.get("query_string", b"").decode()
            for part in query.split("&"):
                if part.startswith("token="):
                    bearer = part[6:]
                    break

        creds = _token_credentials.get(bearer)
        if not creds:
            base = _get_base_url()
            cors_plus_auth = {
                **_CORS_HEADERS,
                "WWW-Authenticate": f'Bearer realm="AI Ark MCP", resource_metadata="{base}/.well-known/oauth-protected-resource"',
            }
            response = JSONResponse({"error": "unauthorized"}, status_code=401, headers=cors_plus_auth)
            await response(scope, receive, send)
            return

        _ctx_api_key.set(creds["api_key"])
        await self._with_cors(scope, receive, send)

    async def _with_cors(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def cors_send(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                for k, v in _CORS_HEADERS.items():
                    headers.append((k.lower().encode(), v.encode()))
                message = {**message, "headers": headers}
            await send(message)
        await self.app(scope, receive, cors_send)


# ── App assembly ──────────────────────────────────────────────────────────────

def _build_app() -> ASGIApp:
    import contextlib
    from mcp.server.sse import SseServerTransport

    http_app = mcp.streamable_http_app()
    mcp_handler = http_app.routes[0].app

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with mcp_handler.session_manager.run():
            yield

    sse = SseServerTransport("/messages")
    mcp_server = mcp._mcp_server

    async def handle_sse(request: Request):
        async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
            await mcp_server.run(streams[0], streams[1], mcp_server.create_initialization_options())

    routes = [
        Route("/.well-known/oauth-protected-resource", oauth_protected_resource),
        Route("/.well-known/oauth-protected-resource/{path:path}", oauth_protected_resource),
        Route("/.well-known/oauth-authorization-server", oauth_authorization_server),
        Route("/.well-known/oauth-authorization-server/{path:path}", oauth_authorization_server),
        Route("/register", register, methods=["POST"]),
        Route("/authorize", authorize, methods=["GET", "POST"]),
        Route("/token", token_endpoint, methods=["POST"]),
        Route("/webhook/{track_id}", webhook_receiver, methods=["POST"]),
        Route("/mcp", mcp_handler),
        Route("/sse", handle_sse, methods=["GET"]),
        Mount("/messages", app=sse.handle_post_message),
    ]

    app = Starlette(routes=routes, lifespan=lifespan)
    app = ArkAuthMiddleware(app)
    return app


if __name__ == "__main__":
    import uvicorn

    app = _build_app()
    config = uvicorn.Config(
        app,
        host=mcp.settings.host,
        port=mcp.settings.port,
        log_level="info",
    )
    server = uvicorn.Server(config)
    server.run()
