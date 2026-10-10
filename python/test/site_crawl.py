"""Site crawler tests: offline, with a fake fetcher."""

import pytest

from crawler import Blocked, is_site_seed
from site_crawl import crawl_site, host_key, main, page_links


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
    import site_crawl

    monkeypatch.setattr(site_crawl, "HttpFetcher", lambda **_: FakeFetcher({
        SITE: '<a href="novel.pdf">မြန်မာဝတ္ထု</a>',
    }))
    monkeypatch.setattr(site_crawl.time, "sleep", lambda _s: None)
    code = main([SITE, "--delay=0", "--jitter=0",
                 f"--entry-list={tmp_path/'e.txt'}", f"--scribd-list={tmp_path/'s.txt'}"])
    assert code == 0
    assert SITE + "novel.pdf" in (tmp_path / "e.txt").read_text(encoding="utf8")


def _search_setup(monkeypatch, tmp_path, pages, sites, downloads, gaps=None):
    """pages: {offset: (results, more)}; sites: {url: html}. Returns the list of search calls."""
    import site_crawl

    calls = []

    def fake_brave(query, offset, api_key, timeout=30.0):
        calls.append((query, offset))
        return pages.get(offset, ([], False))

    monkeypatch.setenv("BRAVE_API_KEY", "test-key")
    monkeypatch.setattr(site_crawl, "brave_search", fake_brave)
    monkeypatch.setattr(site_crawl, "HttpFetcher", lambda **_: FakeFetcher(sites))
    monkeypatch.setattr(site_crawl, "download_new", lambda entry_list: downloads.append(entry_list))
    monkeypatch.setattr(site_crawl.time, "sleep", lambda s: gaps.append(s) if gaps is not None else None)
    return calls


def _args(tmp_path, *extra):
    return [f"--entry-list={tmp_path/'e.txt'}", f"--scribd-list={tmp_path/'s.txt'}",
            "--delay=0", "--jitter=0", "--search-terms=Myanmar PDF", *extra]


def test_no_url_follows_results_in_order_and_downloads_after_each_site(tmp_path, monkeypatch):
    downloads = []
    pages = {0: ([{"url": "https://first.example.com/", "title": "first"},
                  {"url": "https://second.example.com/", "title": "second"}], True),
             1: ([{"url": "https://third.example.com/", "title": "third"}], False)}
    sites = {
        "https://first.example.com/": '<a href="a.pdf">Myanmar A</a>',
        "https://second.example.com/": '<a href="b.pdf">Myanmar B</a>',
        "https://third.example.com/": '<a href="c.pdf">Myanmar C</a>',
    }
    calls = _search_setup(monkeypatch, tmp_path, pages, sites, downloads)
    code = main(_args(tmp_path))
    assert code == 0
    assert calls == [("Myanmar PDF", 0), ("Myanmar PDF", 1)], "stops when more_results_available is false"
    assert len(downloads) == 3, "each site with a PDF is downloaded before the next result"
    entries = (tmp_path / "e.txt").read_text(encoding="utf8")
    assert "a.pdf" in entries and "b.pdf" in entries and "c.pdf" in entries


def test_no_url_waits_between_search_requests(tmp_path, monkeypatch):
    gaps = []
    pages = {0: ([{"url": "https://only.example.com/", "title": "x"}], True),
             1: ([], False)}
    _search_setup(monkeypatch, tmp_path, pages, {"https://only.example.com/": "<p>none</p>"}, [], gaps)
    main(_args(tmp_path, "--search-gap=30"))
    assert gaps and all(g > 0 for g in gaps), "a second search is never sent right after the first"


def test_no_url_stops_on_brave_rate_limit(tmp_path, monkeypatch):
    import site_crawl

    monkeypatch.setenv("BRAVE_API_KEY", "test-key")

    def limited(query, offset, api_key, timeout=30.0):
        raise site_crawl.SearchStopped("Brave rate limit reached (HTTP 429).")

    monkeypatch.setattr(site_crawl, "brave_search", limited)
    code = main(_args(tmp_path))
    assert code == 1


def test_no_url_without_a_key_does_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    assert main(_args(tmp_path)) == 1


def test_brave_search_parses_results_and_more_flag(monkeypatch):
    import site_crawl

    class Response:
        status_code = 200

        def json(self):
            return {"web": {"results": [{"url": "https://x.example.com/", "title": "X"}]},
                    "query": {"more_results_available": True}}

    monkeypatch.setattr(site_crawl.httpx, "get", lambda *a, **k: Response())
    assert site_crawl.brave_search("q", 0, "k") == ([{"url": "https://x.example.com/", "title": "X"}], True)
