"""Offline tests for search_crawl.py.

The extraction tests run against the saved result pages in js/test/fixtures/
(shared with the JavaScript crawler), so no network access is needed. The
Mojeek, Brave, SearXNG and Bing fixtures mirror real engine markup.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from search_crawl import (  # noqa: E402
    ENGINES,
    Blocked,
    HttpFetcher,
    RateLimited,
    build_queries,
    classify_page,
    extract_results,
    is_scribd_document_url,
    matches_book_candidate,
    parse_retry_after,
    unwrap_result_url,
)

FIXTURES = Path(__file__).resolve().parent.parent / "js" / "test" / "fixtures"


FIXTURE_NAMES = {"bing": "bing-li-algo"}


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf8")


PAGES = {
    "mojeek": "https://www.mojeek.com/search?q=test",
    "searx": "https://searx.be/search?q=test&pageno=1",
    "brave": "https://search.brave.com/search?q=test&spellcheck=0",
    "bing": "https://www.bing.com/search?q=test&count=10&first=1",
    "google": "https://www.google.com/search?q=test&num=10&hl=en&start=0",
    "duckduckgo": "https://html.duckduckgo.com/html/?q=test&s=0",
}


def results_for(name: str):
    engine = ENGINES[name]
    fixture_name = FIXTURE_NAMES.get(name, f"{name}-results")
    results, used_fallback = extract_results(fixture(f"{fixture_name}.html"), PAGES[name], engine)
    return results, used_fallback


@pytest.mark.parametrize("engine", sorted(ENGINES))
def test_extracts_results_from_every_engine(engine):
    results, used_fallback = results_for(engine)
    assert results, f"no results parsed for {engine}"
    assert used_fallback is False, f"{engine} needed the generic fallback"
    for result in results:
        assert result["url"].startswith("http")
        assert result["title"]


def test_mojeek_uses_the_real_container_class():
    """Mojeek wraps results in ul.results / ul.results-standard, not a fixed class."""
    html = fixture("mojeek-results.html")
    assert 'ul class="results"' in html or 'ul class="results-standard"' in html
    results, _ = extract_results(html, PAGES["mojeek"], ENGINES["mojeek"])
    # The third result is an unrelated PDF; it must still be parsed (filtering
    # happens later).
    assert len(results) == 3
    assert results[0]["url"] == "https://www.scribd.com/document/451498539/Trigonometry-angle-value-table-pdf"
    assert results[0]["title"] == "Trigonometry angle value table - pdf"


def test_brave_skips_non_web_snippets():
    results, _ = extract_results(fixture("brave-results.html"), PAGES["brave"], ENGINES["brave"])
    assert [result["url"] for result in results] == [
        "https://www.scribd.com/document/451498539/Trigonometry-angle-value-table-pdf",
        "https://files.example.org/burma/myanmar-poetry.pdf",
    ]
    assert results[1]["title"] == "မြန်မာကဗျာများ"


def test_scribd_document_urls():
    for url in (
        "https://www.scribd.com/document/451498539/Trigonometry-angle-value-table-pdf",
        "https://scribd.com/document/123/Title",
        "https://www.scribd.com/doc/1234567/Myanmar-Poems",
        "https://www.scribd.com/book/987654321/Some-Book",
    ):
        assert is_scribd_document_url(url), url
    for url in (
        "https://www.scribd.com/",
        "https://www.scribd.com/search?query=myanmar",
        "https://www.scribd.com/user/12345/name",
        "https://example.com/document/12345/Title",
    ):
        assert not is_scribd_document_url(url), url


def test_book_candidate_matching():
    assert matches_book_candidate("https://example.org/a/myanmar-novel.pdf", "", "filename")
    assert matches_book_candidate("https://example.org/a/%E1%80%99%E1%80%BC%E1%80%94%E1%80%BA%E1%80%99%E1%80%AC.pdf", "")
    assert not matches_book_candidate("https://example.org/a/random.pdf", "")
    assert not matches_book_candidate("https://example.org/a/myanmar.html", "")
    # "loose" also accepts the title; "filename" does not.
    assert matches_book_candidate("https://files.example.org/12345.pdf", "မြန်မာကဗျာ", "loose")
    assert not matches_book_candidate("https://files.example.org/12345.pdf", "မြန်မာကဗျာ", "filename")


def test_redirect_unwrapping():
    assert unwrap_result_url(
        "https://www.bing.com/ck/a?!&&p=abc&u=a1aHR0cHM6Ly93d3cuc2NyaWJkLmNvbS9kb2N1bWVudC8xMjMvVGl0bGU&ntb=1",
        PAGES["bing"],
    ) == "https://www.scribd.com/document/123/Title"
    assert unwrap_result_url("//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fa.pdf", PAGES["duckduckgo"]) == "https://example.org/a.pdf"
    assert unwrap_result_url("/url?q=https://example.org/a.pdf&sa=U", PAGES["google"]) == "https://example.org/a.pdf"
    assert unwrap_result_url("#anchor", PAGES["bing"]) is None
    assert unwrap_result_url("mailto:someone@example.org", PAGES["bing"]) is None


def test_no_results_page_yields_nothing():
    """Footer links must not be reported as results on an empty page."""
    for name in ("bing-no-results", "brave-no-results"):
        html = fixture(f"{name}.html")
        results, _ = extract_results(html, PAGES["bing"], ENGINES["bing"])
        assert results == []
        assert classify_page(html, len(results)) == "no-results"


def test_page_classification():
    assert classify_page(fixture("brave-cloudflare.html"), 0) == "blocked"
    assert classify_page(fixture("google-consent.html"), 0) == "blocked"
    assert classify_page(fixture("bing-empty-unknown.html"), 0) == "empty"
    assert classify_page(fixture("bing-li-algo.html"), 3) == "results"


def test_mojeek_page_one_omits_s():
    assert ENGINES["mojeek"].page_url("myanmar pdf", 0) == "https://www.mojeek.com/search?q=myanmar+pdf"
    assert ENGINES["mojeek"].page_url("myanmar pdf", 2) == "https://www.mojeek.com/search?q=myanmar+pdf&s=20"


def test_site_queries_use_a_bare_domain():
    queries = build_queries(["Myanmar PDF"])
    assert "site:scribd.com Myanmar PDF" in queries
    assert not any("site:scribd.com/doc" in query for query in queries)


def test_retry_after_parsing():
    assert parse_retry_after("30") == 30.0
    assert parse_retry_after(None) == 0.0
    assert parse_retry_after("not-a-date") == 0.0


def test_http_fetcher_raises_rate_limited(monkeypatch):
    class Response:
        status_code = 429
        reason = "Too Many Requests"
        headers = {"Retry-After": "1"}

        def __init__(self):
            self.ok = False

    fetcher = HttpFetcher(retries=1)
    monkeypatch.setattr(fetcher.session, "get", lambda *a, **k: Response())
    with pytest.raises(RateLimited):
        fetcher.fetch("https://www.mojeek.com/search?q=test")


def test_http_fetcher_raises_blocked(monkeypatch):
    class Response:
        status_code = 403
        reason = "Forbidden"
        headers: dict[str, str] = {}

        def __init__(self):
            self.ok = False

    fetcher = HttpFetcher(retries=1)
    monkeypatch.setattr(fetcher.session, "get", lambda *a, **k: Response())
    with pytest.raises(Blocked):
        fetcher.fetch("https://www.mojeek.com/search?q=test")
