"""Site crawler tests: offline, with a fake fetcher."""

import time

import pytest

from crawler import Blocked, is_site_seed
import dsite
from dsite import crawl_site, host_key, main, page_links


class FakeFetcher:
    def __init__(self, pages, robots=None, blocked=()):
        self.pages = pages
        self.robots = robots
        self.blocked = set(blocked)
        self.fetched = []

    def fetch(self, url):
        if url.endswith("/robots.txt"):
            if self.robots is None:
                raise RuntimeError("no robots.txt")
            return self.robots, url
        self.fetched.append(url)
        if url in self.blocked:
            raise Blocked(f"HTTP 403 for {url}")
        if url not in self.pages:
            raise RuntimeError(f"404 {url}")
        return self.pages[url], url


SITE = "https://books.example.org/"


def run(fetcher, seed=SITE, **kwargs):
    entries, scribd, not_kept = {}, {}, []
    options = dict(max_pages=50, max_depth=3, match_mode="loose", delay=0, jitter=0,
                   sleep=lambda _s: None, log=lambda _m: None)
    options.update(kwargs)
    stats = crawl_site(seed, fetcher, entries=entries, scribd=scribd, not_kept=not_kept, **options)
    return stats, entries, scribd, not_kept


def test_follows_same_site_and_collects_matching_pdfs():
    fetcher = FakeFetcher({
        SITE: '<a href="burmese/novel.pdf">မြန်မာဝတ္ထု</a><a href="/shelf/">Shelf</a>',
        SITE + "shelf/": '<a href="../myanmar-poems.pdf">Myanmar poems</a>',
    })
    stats, entries, _, _ = run(fetcher)
    assert set(entries) == {SITE + "burmese/novel.pdf", SITE + "myanmar-poems.pdf"}
    assert stats["pages"] == 2


def test_other_domains_are_not_followed():
    fetcher = FakeFetcher({SITE: '<a href="https://elsewhere.example.net/x">away</a>'})
    run(fetcher)
    assert fetcher.fetched == [SITE]


def test_scribd_goes_to_its_own_register_and_is_not_fetched():
    doc = "https://www.scribd.com/document/123/Burmese-Book"
    fetcher = FakeFetcher({SITE: f'<a href="{doc}">Burmese book</a>'})
    _, entries, scribd, _ = run(fetcher)
    assert doc in scribd and not entries
    assert doc not in fetcher.fetched


def test_pdfs_without_a_burmese_match_are_reported_not_kept():
    fetcher = FakeFetcher({SITE: '<a href="algebra.pdf">Algebra</a>'})
    _, entries, _, not_kept = run(fetcher)
    assert not entries
    assert not_kept == [("Algebra", SITE + "algebra.pdf")]


def test_robots_txt_disallow_is_respected():
    fetcher = FakeFetcher(
        {SITE: '<a href="private/a.html">x</a>', SITE + "private/a.html": '<a href="p.pdf">Myanmar</a>'},
        robots="User-agent: *\nDisallow: /private/\n",
    )
    _, entries, _, _ = run(fetcher)
    assert SITE + "private/a.html" not in fetcher.fetched
    assert not entries


def test_page_cap_is_respected():
    pages = {SITE: "".join(f'<a href="p{i}.html">x</a>' for i in range(20))}
    for i in range(20):
        pages[SITE + f"p{i}.html"] = "<p>nothing</p>"
    fetcher = FakeFetcher(pages)
    stats, _, _, _ = run(fetcher, max_pages=5)
    assert stats["pages"] == 5


def test_depth_limit_is_respected():
    fetcher = FakeFetcher({SITE: '<a href="a.html">a</a>', SITE + "a.html": '<a href="b.html">b</a>',
                           SITE + "b.html": '<a href="c.pdf">Myanmar</a>'})
    _, entries, _, _ = run(fetcher, max_depth=1)
    assert not entries


def test_a_blocked_site_is_abandoned_at_once():
    fetcher = FakeFetcher({SITE: '<a href="a.html">a</a>', SITE + "a.html": "<p>x</p>"}, blocked=[SITE])
    stats, entries, _, _ = run(fetcher)
    assert stats["pages"] == 0 and not entries
    assert fetcher.fetched == [SITE]


def test_page_links_resolves_relative_and_base_tag():
    html = '<base href="https://books.example.org/library/"><a href="x.pdf">X</a><a href="#top">top</a>'
    assert page_links(html, SITE) == [("https://books.example.org/library/x.pdf", "X")]


def test_host_key_ignores_www():
    assert host_key("https://www.example.org/a") == host_key("https://example.org/b") == "example.org"


def test_search_results_become_sites_only_when_they_are_ordinary_pages():
    assert is_site_seed("https://books.example.org/shelf/index.html")
    assert not is_site_seed("https://www.mojeek.com/search?q=x")
    assert not is_site_seed("https://www.scribd.com/document/1/x")
    assert not is_site_seed("https://files.example.org/book.pdf")


def test_main_with_a_url_writes_the_entry_list(tmp_path, monkeypatch):
    monkeypatch.setattr(dsite, "HttpFetcher", lambda **_: FakeFetcher({
        SITE: '<a href="novel.pdf">မြန်မာဝတ္ထု</a>',
    }))
    monkeypatch.setattr(dsite.time, "sleep", lambda _s: None)
    code = main([SITE, "--delay=0", "--jitter=0",
                 f"--entry-list={tmp_path/'e.txt'}", f"--scribd-list={tmp_path/'s.txt'}"])
    assert code == 0
    assert SITE + "novel.pdf" in (tmp_path / "e.txt").read_text(encoding="utf8")


def _search_setup(monkeypatch, tmp_path, pages, sites, downloads, gaps=None):
    """pages: {(query, start): (results, more)}; sites: {url: html}. Returns the list of search calls."""
    calls = []

    def fake_search(query, start, api_key, timeout=60.0):
        calls.append((query, start))
        return pages.get((query, start), ([], False))

    monkeypatch.setenv("SERPAPI", "test-key")
    monkeypatch.setattr(dsite, "ENV_FILE", tmp_path / "no-such.env")
    monkeypatch.setattr(dsite, "serpapi_search", fake_search)
    monkeypatch.setattr(dsite, "HttpFetcher", lambda **_: FakeFetcher(sites))
    monkeypatch.setattr(dsite, "download_new", lambda entry_list: downloads.append(entry_list))
    monkeypatch.setattr(dsite.time, "sleep", lambda s: gaps.append(s) if gaps is not None else None)
    return calls


def _args(tmp_path, *extra):
    return [f"--entry-list={tmp_path/'e.txt'}", f"--scribd-list={tmp_path/'s.txt'}",
            "--delay=0", "--jitter=0", "--search-terms=Myanmar PDF", f"--usage-file={tmp_path/'usage.json'}", *extra]


def test_default_search_terms_are_the_four_approved_keywords():
    terms = dsite.parse_args([]).search_terms.split(",")
    assert terms == ["myanmar books download", "myanmar ebooks download", "Myanmar PDF free download",
                     "free မြန်မာ pdf စာအုပ်များ"]
    assert dsite.parse_args([]).search_pages == 1, "one page per keyword by default"
    assert dsite.parse_args([]).monthly_limit == 250


def test_no_url_follows_results_in_order_and_downloads_after_each_site(tmp_path, monkeypatch):
    downloads = []
    pages = {("Myanmar PDF", 0): ([{"url": "https://first.example.com/", "title": "first"},
                                   {"url": "https://second.example.com/", "title": "second"}], False)}
    sites = {
        "https://first.example.com/": '<a href="a.pdf">Myanmar A</a>',
        "https://second.example.com/": '<a href="b.pdf">Myanmar B</a>',
    }
    calls = _search_setup(monkeypatch, tmp_path, pages, sites, downloads)
    code = main(_args(tmp_path))
    assert code == 0
    assert calls == [("Myanmar PDF", 0)]
    assert len(downloads) == 2, "each site with a PDF is downloaded before the next result"
    entries = (tmp_path / "e.txt").read_text(encoding="utf8")
    assert "a.pdf" in entries and "b.pdf" in entries


def test_one_search_per_keyword_and_no_pagination_by_default(tmp_path, monkeypatch):
    pages = {("one", 0): ([], True), ("one", 10): ([], True), ("two", 0): ([], False)}
    calls = _search_setup(monkeypatch, tmp_path, pages, {}, [])
    main(_args(tmp_path, "--search-terms=one,two"))
    assert calls == [("one", 0), ("two", 0)], "page 1 only, no next page unless --search-pages allows it"


def test_search_pages_flag_follows_pages_while_engine_says_more(tmp_path, monkeypatch):
    hit = [{"url": "https://x.example.com/", "title": "x"}]
    pages = {("one", 0): (hit, True), ("one", 10): (hit, True), ("one", 20): (hit, False)}
    calls = _search_setup(monkeypatch, tmp_path, pages, {}, [])
    main(_args(tmp_path, "--search-terms=one", "--search-pages=5"))
    assert calls == [("one", 0), ("one", 10), ("one", 20)], "stops when SerpApi has no next page"


def test_no_url_waits_between_search_requests(tmp_path, monkeypatch):
    gaps = []
    pages = {("Myanmar PDF", 0): ([], False)}
    calls = _search_setup(monkeypatch, tmp_path, pages, {}, [], gaps)
    main(_args(tmp_path, "--search-terms=Myanmar PDF,Myanmar PDF", "--search-gap=60"))
    assert len(calls) == 2
    assert gaps and all(0 < g <= 60 for g in gaps), "a second search waits up to the 60 s gap, never zero"


def test_no_url_stops_on_serpapi_error(tmp_path, monkeypatch):
    monkeypatch.setenv("SERPAPI", "test-key")
    monkeypatch.setattr(dsite, "ENV_FILE", tmp_path / "no-such.env")

    def refused(query, start, api_key, timeout=60.0):
        raise dsite.SearchStopped("SerpApi rate limit reached (HTTP 429).")

    monkeypatch.setattr(dsite, "serpapi_search", refused)
    assert main(_args(tmp_path, "--search-terms=one,two")) == 1


def test_no_url_without_a_key_does_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("SERPAPI", raising=False)
    monkeypatch.setattr(dsite, "ENV_FILE", tmp_path / "no-such.env")
    calls = _search_setup(monkeypatch, tmp_path, {}, {}, [])
    monkeypatch.delenv("SERPAPI", raising=False)
    assert main(_args(tmp_path)) == 1
    assert calls == []


def test_key_is_read_from_dotenv_when_environment_is_empty(tmp_path, monkeypatch):
    monkeypatch.delenv("SERPAPI", raising=False)
    env = tmp_path / ".env"
    env.write_text("# keys\nOTHER=1\nSERPAPI=\"abc123\"\n", encoding="utf8")
    assert dsite.load_serpapi_key(env) == "abc123"
    monkeypatch.setenv("SERPAPI", "from-env")
    assert dsite.load_serpapi_key(env) == "from-env", "the environment wins over .env"


def test_run_is_refused_when_planned_searches_exceed_the_monthly_budget(tmp_path, monkeypatch):
    import json

    usage = tmp_path / "usage.json"
    usage.write_text(json.dumps({"month": time.strftime("%Y-%m"), "used": 249}), encoding="utf8")
    calls = _search_setup(monkeypatch, tmp_path, {}, {}, [])
    code = main(_args(tmp_path, "--search-terms=a,b", f"--usage-file={usage}"))
    assert code == 1
    assert calls == [], "2 searches needed, 1 left: nothing is sent"


def test_budget_counts_each_successful_search(tmp_path, monkeypatch):
    import json

    pages = {("one", 0): ([], False), ("two", 0): ([], False)}
    _search_setup(monkeypatch, tmp_path, pages, {}, [])
    main(_args(tmp_path, "--search-terms=one,two"))
    data = json.loads((tmp_path / "usage.json").read_text(encoding="utf8"))
    assert data["used"] == 2


def test_budget_resets_in_a_new_month(tmp_path):
    import json
    path = tmp_path / "usage.json"
    path.write_text(json.dumps({"month": "2000-01", "used": 250}), encoding="utf8")
    assert dsite.SearchBudget(path, 250).remaining() == 250


def test_serpapi_search_parses_results_and_next_page(monkeypatch):
    seen = {}

    class Response:
        status_code = 200

        def json(self):
            return {"organic_results": [{"link": "https://x.example.com/", "title": "X"}, {"title": "no link"}],
                    "serpapi_pagination": {"next": "https://serpapi.com/search.json?start=10"}}

    def fake_get(url, params=None, timeout=None):
        seen.update(url=url, params=params)
        return Response()

    monkeypatch.setattr(dsite.httpx, "get", fake_get)
    results, more = dsite.serpapi_search("q", 10, "k")
    assert results == [{"url": "https://x.example.com/", "title": "X"}]
    assert more is True
    assert seen["url"] == "https://serpapi.com/search.json"
    assert seen["params"] == {"engine": "google", "q": "q", "api_key": "k", "start": 10}


def test_serpapi_search_stops_on_refused_key_and_rate_limit(monkeypatch):
    class Response:
        def __init__(self, status):
            self.status_code = status

        def json(self):
            return {"error": "Invalid API key"}

    for status in (401, 403, 429):
        monkeypatch.setattr(dsite.httpx, "get", lambda *a, _s=status, **k: Response(_s))
        with pytest.raises(dsite.SearchStopped):
            dsite.serpapi_search("q", 0, "k")


def test_key_file_variants_are_all_read(tmp_path, monkeypatch):
    monkeypatch.delenv("SERPAPI", raising=False)
    cases = {
        "bom.env": "\ufeffSERPAPI=abc\n",                      # Windows editors add a BOM
        "export.env": "export SERPAPI=abc\n",
        "quoted.env": 'SERPAPI="abc"\r\n',                      # CRLF line ends and quotes
        "spaced.env": "  SERPAPI = abc  \n",
    }
    for name, text in cases.items():
        path = tmp_path / name
        path.write_text(text, encoding="utf8")
        assert dsite.load_serpapi_key(path) == "abc", name
    utf16 = tmp_path / "utf16.env"
    utf16.write_bytes("SERPAPI=abc\n".encode("utf-16"))      # PowerShell `>` writes UTF-16
    assert dsite.load_serpapi_key(utf16) == "abc"


def test_empty_value_is_not_a_key(tmp_path, monkeypatch):
    monkeypatch.delenv("SERPAPI", raising=False)
    path = tmp_path / ".env"
    path.write_text("SERPAPI=\n", encoding="utf8")
    assert dsite.load_serpapi_key(path) == ""


def test_missing_key_message_says_what_is_wrong(tmp_path):
    missing = tmp_path / "nope.env"
    assert "does not exist" in dsite.explain_missing_key(missing)
    present = tmp_path / ".env"
    present.write_text("OTHER=1\n", encoding="utf8")
    assert "has no line of the form SERPAPI=" in dsite.explain_missing_key(present)


def test_a_found_pdf_is_logged_and_saved_at_once():
    saves, logs = [], []
    fetcher = FakeFetcher({SITE: '<a href="novel.pdf">မြန်မာဝတ္ထု</a>'})
    run(fetcher, log=logs.append, save=lambda: saves.append(1))
    assert saves, "the lists are saved when the page adds a link, not only at the end of the site"
    assert any("PDF/DOCX found: https://books.example.org/novel.pdf" in line for line in logs)
