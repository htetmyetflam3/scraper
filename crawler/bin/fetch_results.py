"""Fetch SerpApi result links once and save them for offline crawling."""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
from serpapi import SerpApiError, serpapi_search

REPO_ROOT = Path(__file__).resolve().parents[2]
SCAN_DIR = REPO_ROOT / "scan"
OUTPUT_FILE = SCAN_DIR / "search_results.json"
LEGACY_OUTPUT_FILE = REPO_ROOT / "crawler" / "search_results.json"
DEFAULT_KEYWORDS = (
    "myanmar books download",
    "myanmar ebooks download",
    "Myanmar PDF free download",
    "free မြန်မာ pdf စာအုပ်များ",
    "မြန်မာpdf free download",
    "free မြန်မာဝတ္ထုများ"
)
SEARCH_PAGES_PER_KEYWORD = 2
RESULTS_PER_PAGE = 10


def _read_api_key(explicit_key: str | None) -> str:
    if explicit_key and explicit_key.strip():
        return explicit_key.strip()
    environment_key = os.environ.get("SERPAPI", "").strip()
    if environment_key:
        return environment_key

    env_file = REPO_ROOT / ".env"  # repo root, next to main.py
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


def _load_saved(output_file: Path) -> list[dict]:
    """Previously saved results, or [] when no usable file exists yet."""
    if not output_file.exists() and output_file == OUTPUT_FILE:
        legacy = LEGACY_OUTPUT_FILE
        if legacy.exists():
            print(
                f"[migrate] reading existing results from {legacy}; "
                f"will save merged output to {output_file}",
                flush=True,
            )
            output_file = legacy
    if not output_file.exists():
        return []
    try:
        loaded = json.loads(output_file.read_text(encoding="utf-8"))
    except ValueError as error:
        raise ValueError(
            f"Cannot parse {output_file} as JSON: {error}. "
            "Fix or delete it; refusing to overwrite."
        ) from error
    if not isinstance(loaded, list):
        raise ValueError(
            f"Cannot parse {output_file}: expected a JSON list. "
            "Fix or delete it; refusing to overwrite."
        )
    return [item for item in loaded if isinstance(item, dict)]


def fetch_results(
    api_key: str | None = None,
    output_file: str | Path = OUTPUT_FILE,
    keywords: list[str] | tuple[str, ...] | None = None,
) -> list[dict]:
    """Run the keyword searches and save every returned link as JSON.

    Each keyword uses at most two SerpApi pages. Keywords already
    recorded in the output file are skipped, never re-fetched, so adding
    new keywords costs only their own API calls; new results are appended
    to the saved ones. Nothing is filtered or deduplicated: the file keeps
    ``url``, ``title``, ``keyword`` and ``page`` per result so the crawl
    stage (and hand-testing) sees the raw search output. Extra keys in
    hand-written files are ignored.
    """
    key = _read_api_key(api_key)
    out = Path(output_file)
    existing = _load_saved(out)
    saved_keywords = {
        str(item.get("keyword", "")).strip()
        for item in existing
        if str(item.get("keyword", "")).strip()
    }
    run_keywords = [
        keyword
        for keyword in (DEFAULT_KEYWORDS if keywords is None else keywords)
        if keyword.strip()
    ]
    collected: list[dict] = []
    for keyword in run_keywords:
        if keyword.strip() in saved_keywords:
            kept = sum(
                1
                for item in existing
                if str(item.get("keyword", "")).strip() == keyword.strip()
            )
            print(
                f"[skip] {keyword!r} already in {out} "
                f"({kept} saved result(s)); no API call",
                flush=True,
            )
            continue
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

    merged = existing + collected
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(merged, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(
        f"[done] {len(collected)} new, {len(merged)} total in {out}",
        flush=True,
    )
    return merged
