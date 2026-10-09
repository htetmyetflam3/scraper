"""Offline tests for search_crawl.py.

The extraction tests run against the saved result pages in test/fixtures/,
so no network access is needed. The
Mojeek, Brave, SearXNG and Bing fixtures mirror real engine markup.
"""

from __future__ import annotations

import sys
from contextlib import ExitStack
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # python/

from crawler import (  # noqa: E402
    CHROMIUM_NAMES,
    ENGINES,
    Blocked,
    ChromiumFetcher,
    HttpFetcher,
    RateLimited,
    find_chromium,
    build_queries,
    classify_page,
    extract_results,
    is_scribd_document_url,
    make_fetcher,
    matches_book_candidate,
    parse_args,
    parse_retry_after,
    unwrap_result_url,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


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


def test_mojeek_bot_403_page_is_blocked_not_empty():
    # The page Mojeek returned to a chromium run: "403 - Forbidden ... appears to be
    # sending automated queries". It used to classify as "empty" and be retried.
    html = (
        "<html><head><title>403 - Forbidden</title></head><body><h1>403 - Forbidden</h1>"
        "<p>Sorry your network appears to be sending automated queries so we can't process "
        "your search at this time.</p><a href='http://localhost:8158/about/contact'>contact us</a>"
        "</body></html>"
    )
    assert classify_page(html, 0) == "blocked"


def test_mojeek_altcha_challenge_is_blocked_not_zero_results():
    # Reconstructed from the text Mojeek served for real queries (title "Captcha",
    # body "Verification required", "Protected by ALTCHA"). Before this fix it was
    # classified "empty" and reported as "0 matches".
    html = (
        "<html><head><title>Captcha</title></head><body>"
        "<h1>Verification required</h1>"
        "<p>Please complete the challenge to continue.</p>"
        "<p>Protected by ALTCHA</p><p>Waiting for verification.</p>"
        "</body></html>"
    )
    assert classify_page(html, 0) == "blocked"
    assert classify_page(fixture("bing-li-algo.html"), 3) == "results"


def test_mojeek_page_one_omits_s():
    assert ENGINES["mojeek"].page_url("myanmar pdf", 0) == "https://www.mojeek.com/search?q=myanmar+pdf"
    # s is a 1-based result index, so page three starts at result 21.
    assert ENGINES["mojeek"].page_url("myanmar pdf", 2) == "https://www.mojeek.com/search?q=myanmar+pdf&s=21"


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



# --------------------------------------------------------------------------- #
# Headless browser without the Playwright wheel
# --------------------------------------------------------------------------- #

def make_fake_chromium(directory: Path, html: str) -> Path:
    """A stand-in for `chromium --headless --dump-dom`: prints HTML, ignores flags."""
    page = directory / "page.html"
    page.write_text(html, encoding="utf8")
    script = directory / "chromium"
    script.write_text(f'#!/bin/sh\necho "fake-chromium $*" >&2\ncat {page}\n', encoding="utf8")
    script.chmod(0o755)
    return script


def test_find_chromium_prefers_explicit_path(tmp_path, monkeypatch):
    binary = tmp_path / "my-chrome"
    binary.write_text("#!/bin/sh\n", encoding="utf8")
    monkeypatch.setattr("crawler.shutil.which", lambda _name: "/usr/bin/chromium")
    assert find_chromium(str(binary)) == [str(binary)]


def test_find_chromium_accepts_a_full_command(tmp_path, monkeypatch):
    """--browser-path may be a command, e.g. a browser inside a container."""
    monkeypatch.setattr("crawler.shutil.which", lambda name: f"/usr/bin/{name}" if name == "docker" else None)
    assert find_chromium("docker run --rm --entrypoint chromium image:tag") == [
        "/usr/bin/docker", "run", "--rm", "--entrypoint", "chromium", "image:tag",
    ]


def test_find_chromium_reads_env_then_path(tmp_path, monkeypatch):
    binary = tmp_path / "chromium"
    binary.write_text("#!/bin/sh\n", encoding="utf8")
    monkeypatch.setenv("CHROMIUM_PATH", str(binary))
    monkeypatch.setattr("crawler.shutil.which", lambda _name: "/usr/bin/something")
    assert find_chromium() == [str(binary)]

    monkeypatch.delenv("CHROMIUM_PATH")
    monkeypatch.setattr("crawler.shutil.which", lambda name: f"/usr/bin/{name}" if name == CHROMIUM_NAMES[0] else None)
    assert find_chromium() == [f"/usr/bin/{CHROMIUM_NAMES[0]}"]


def test_find_chromium_returns_none_when_absent(monkeypatch):
    monkeypatch.delenv("CHROMIUM_PATH", raising=False)
    monkeypatch.setattr("crawler.shutil.which", lambda _name: None)
    assert find_chromium() is None


def test_chromium_command_orders_flags_correctly(tmp_path):
    binary = tmp_path / "chromium"
    binary.write_text("#!/bin/sh\n", encoding="utf8")
    fetcher = ChromiumFetcher(binary=str(binary))
    with fetcher:
        cmd = fetcher.command("https://example.com/search?q=burmese")
    assert cmd[0].endswith("chromium")
    assert cmd[-2:] == ["--dump-dom", "https://example.com/search?q=burmese"]
    assert "--headless=new" in cmd
    assert "--no-sandbox" in cmd
    assert "--disable-dev-shm-usage" in cmd
    assert any(part.startswith("--virtual-time-budget=") for part in cmd)
    assert any(part.startswith("--user-data-dir=") for part in cmd)
    assert any(part.startswith("--user-agent=") for part in cmd)


def test_chromium_fetcher_parses_dumped_dom(tmp_path):
    html = fixture("mojeek-results.html")
    binary = make_fake_chromium(tmp_path, html)
    with ChromiumFetcher(binary=str(binary)) as fetcher:
        assert isinstance(fetcher, ChromiumFetcher)
        dom, url = fetcher.fetch("https://www.mojeek.com/search?q=myanmar+pdf")
        assert url == "https://www.mojeek.com/search?q=myanmar+pdf"
        results, _ = extract_results(dom, url, ENGINES["mojeek"])
    assert results, "the DOM dumped by the browser must parse like any other page"
    assert all(result["url"].startswith("http") for result in results)


def test_chromium_fetcher_keeps_the_profile_between_pages(tmp_path):
    binary = make_fake_chromium(tmp_path, "<html></html>")
    with ChromiumFetcher(binary=str(binary)) as fetcher:
        profile_flag = [a for a in fetcher.command("https://example.com") if a.startswith("--user-data-dir=")][0]
        assert Path(profile_flag.split("=", 1)[1]).is_dir()
    assert not Path(profile_flag.split("=", 1)[1]).exists(), "profile is cleaned up on exit"


def test_chromium_fetcher_reports_a_broken_browser(tmp_path):
    script = tmp_path / "chromium"
    script.write_text("#!/bin/sh\nexit 7\n", encoding="utf8")
    script.chmod(0o755)
    with ChromiumFetcher(binary=str(script)) as fetcher:
        with pytest.raises(RuntimeError, match="exited 7"):
            fetcher.fetch("https://example.com")


def test_chromium_fetcher_needs_a_binary(tmp_path, monkeypatch):
    monkeypatch.delenv("CHROMIUM_PATH", raising=False)
    monkeypatch.setattr("crawler.shutil.which", lambda _name: None)
    with pytest.raises(RuntimeError, match="No Chromium binary found"):
        ChromiumFetcher()


def test_auto_fetcher_picks_the_system_chromium(tmp_path, monkeypatch):
    """--fetcher=chromium must work with no Playwright installed at all."""
    binary = make_fake_chromium(tmp_path, "<html></html>")
    monkeypatch.setattr("crawler.shutil.which", lambda _name: None)
    args = parse_args(["--fetcher=chromium", "--browser-path", str(binary)])
    with ExitStack() as stack:
        fetcher = make_fetcher(args, ENGINES["mojeek"], stack)
    assert isinstance(fetcher, ChromiumFetcher)


def test_auto_fetcher_falls_back_to_http_without_a_browser(monkeypatch, capsys):
    monkeypatch.delenv("CHROMIUM_PATH", raising=False)
    monkeypatch.setattr("crawler.shutil.which", lambda _name: None)
    monkeypatch.setattr("crawler.BrowserFetcher.__init__", lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("no playwright")))
    args = parse_args(["--fetcher=auto"])
    with ExitStack() as stack:
        fetcher = make_fetcher(args, ENGINES["mojeek"], stack)
    assert isinstance(fetcher, HttpFetcher)
    out = capsys.readouterr().out
    assert "No headless browser available" in out
    assert "No Chromium binary found" in out
