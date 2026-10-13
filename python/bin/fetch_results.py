"""Fetch SerpApi result links once and save them for offline crawling."""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
from serpapi import SerpApiError, serpapi_search

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_FILE = REPO_ROOT / "search_results.json"
DEFAULT_KEYWORDS = (
    "myanmar books download",
    "myanmar ebooks download",
    "Myanmar PDF free download",
    "free မြန်မာ pdf စာအုပ်များ",
)
SEARCH_PAGES_PER_KEYWORD = 2
RESULTS_PER_PAGE = 10


def _read_api_key(explicit_key: str | None) -> str:
    if explicit_key and explicit_key.strip():
        return explicit_key.strip()
    environment_key = os.environ.get("SERPAPI", "").strip()
    if environment_key:
        return environment_key

    env_file = REPO_ROOT / ".env"
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    for line in lines:
        entry = line.strip()
        if entry.startswith("export "):
            entry = entry[7:].lstrip()
        if not entry or entry.startswith("#") or "=" not in entry:
            continue
        name, value = entry.split("=", 1)
        if name.strip() != "SERPAPI":
            continue
        value = value.strip()
        if value[:1] in ('"', "'"):
            closing_quote = value.find(value[0], 1)
            if closing_quote > 0:
                value = value[1:closing_quote]
        else:
            value = value.split("#", 1)[0].strip()
        if value:
            return value
    raise SerpApiError(
        "Set SERPAPI in the environment or in the repository-root .env file."
    )


def fetch_results(
    api_key: str | None = None,
    output_file: str | Path = OUTPUT_FILE,
) -> list[dict]:
    """Run the keyword searches and save every returned link as JSON.

    Each keyword uses at most two SerpApi pages. Nothing is filtered or
    deduplicated: the file keeps ``url``, ``title``, ``keyword`` and
    ``page`` per result so the crawl stage (and hand-testing) sees the
    raw search output. Extra keys in hand-written files are ignored.
    """
    key = _read_api_key(api_key)
    collected: list[dict] = []
    for keyword in DEFAULT_KEYWORDS:
        for page_number in range(SEARCH_PAGES_PER_KEYWORD):
            print(
                f"[search] {keyword!r}, page {page_number + 1}/"
                f"{SEARCH_PAGES_PER_KEYWORD}",
                flush=True,
            )
            try:
                results, has_next_page = serpapi_search(
                    keyword, page_number * RESULTS_PER_PAGE, key
                )
            except SerpApiError:
                raise
            except httpx.HTTPError as error:
                raise SerpApiError(f"SerpApi request failed: {error}") from error
            for result in results:
                collected.append(
                    {
                        "url": result["url"],
                        "title": result.get("title", ""),
                        "keyword": keyword,
                        "page": page_number + 1,
                    }
                )
            print(
                f"[search] received {len(results)} result(s); "
                f"{len(collected)} total so far",
                flush=True,
            )
            if not has_next_page:
                break

    Path(output_file).write_text(
        json.dumps(collected, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return collected


if __name__ == "__main__":
    try:
        saved = fetch_results()
    except (SerpApiError, OSError) as error:
        raise SystemExit(str(error)) from error
    print(f"Wrote {len(saved)} result(s) to {OUTPUT_FILE}")
