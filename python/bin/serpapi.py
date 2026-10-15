"""One SerpApi Google-result request; no query workflow, budget or cache."""

from __future__ import annotations

import httpx

SERPAPI_URL = "https://serpapi.com/search.json"


class SerpApiError(Exception):
    """SerpApi rejected or failed the request."""


def serpapi_search(
    query: str, start: int, api_key: str, timeout: float = 30.0
) -> tuple[list[dict], bool]:
    """One page of Google results: ([{url, title}], more_pages_available)."""
    params = {"engine": "google", "q": query, "api_key": api_key}
    if start:
        params["start"] = start
    response = httpx.get(SERPAPI_URL, params=params, timeout=timeout)
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code in (401, 403):
        raise SerpApiError(
            f"SerpApi refused the key (HTTP {response.status_code}). "
            "Check the supplied API key."
        )
    if response.status_code == 429:
        raise SerpApiError("SerpApi rate limit reached (HTTP 429). Try again later.")
    if response.status_code != 200 or data.get("error"):
        raise SerpApiError(
            f"SerpApi error: {data.get('error') or 'HTTP ' + str(response.status_code)}"
        )
    results = [
        {"url": item["link"], "title": item.get("title", "")}
        for item in data.get("organic_results", [])
        if item.get("link")
    ]
    more = bool((data.get("serpapi_pagination") or {}).get("next"))
    return results, more
