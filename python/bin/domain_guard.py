"""Domain exclusions for search-result seeding and same-site traversal."""

from __future__ import annotations

import urllib.parse

WIKI_DOMAINS = frozenset(
    {
        "wikipedia.org",
        "wikimedia.org",
        "wiktionary.org",
        "wikibooks.org",
        "wikinews.org",
        "wikiquote.org",
        "wikisource.org",
        "wikiversity.org",
        "wikivoyage.org",
        "wikidata.org",
        "mediawiki.org",
        "fandom.com",
        "wikia.com",
        "wiki.gg",
        "wikidot.com",
        "wikiwand.com",
        "miraheze.org",
    }
)

VIDEO_DOMAINS = frozenset({"youtube.com", "youtu.be", "youtube-nocookie.com"})

ARCHIVE_DOMAINS = frozenset({"archive.org"})

# App/software download sites: wrongly indexed by the search engine for
# book queries because of their metatags, but they host no documents.
APP_DOMAINS = frozenset(
    {
        "apple.com",
        "appstore.com",
        "softonic.com",
        "apkpure.com",
        "apkpure.net",
        "uptodown.com",
        "apkmirror.com",
        "aptoide.com",
        "f-droid.org",
        "filehippo.com",
        "softpedia.com",
        "apps.microsoft.com",
    }
)

TELEGRAM_DOMAINS = frozenset({"t.me", "telegram.me", "telegram.org", "telegram.dog"})

GITHUB_DOMAINS = frozenset(
    {
        "github.com",
        "github.io",
        "githubusercontent.com",
        "github.dev",
        "github.blog",
        "github.community",
        "githubassets.com",
        "git.io",
    }
)

# Widely used search engines and their public redirect/result hosts. Engine
# family labels below also catch regional domains such as google.co.uk and
# yandex.ru, which a short suffix list would miss.
SEARCH_DOMAINS = frozenset(
    {
        "google.com",
        "googleusercontent.com",
        "googleadservices.com",
        "g.co",
        "goo.gl",
        "bing.com",
        "bing.net",
        "duckduckgo.com",
        "duck.com",
        "brave.com",
        "mojeek.com",
        "searx.be",
        "searx.info",
        "searx.space",
        "searxng.com",
        "searxng.site",
        "yahoo.com",
        "yahoo.co.jp",
        "yahoo.net",
        "yandex.com",
        "yandex.ru",
        "yandex.net",
        "yandex.by",
        "yandex.kz",
        "yandex.ua",
        "yandex.uz",
        "yandex.az",
        "baidu.com",
        "baidu.cn",
        "baiducontent.com",
        "ecosia.org",
        "startpage.com",
        "ixquick.com",
        "qwant.com",
        "qwant.fr",
        "ask.com",
        "askjeeves.com",
        "aol.com",
        "naver.com",
        "daum.net",
        "sogou.com",
        "so.com",
        "sm.cn",
        "rambler.ru",
        "goo.ne.jp",
        "metager.org",
        "metager.de",
        "marginalia.nu",
        "yacy.net",
        "kiddle.co",
        "kidrex.org",
        "search.com",
        "seznam.cz",
        "search.ch",
        "lycos.com",
        "excite.com",
        "webcrawler.com",
        "metacrawler.com",
        "dogpile.com",
        "infospace.com",
        "hotbot.com",
        "searchencrypt.com",
        "swisscows.com",
        "gibiru.com",
        "kagi.com",
        "you.com",
        "yippy.com",
        "presearch.com",
        "yep.com",
        "wiby.me",
    }
)

SEARCH_ENGINE_LABELS = frozenset(
    {
        "google",
        "yandex",
        "yarndex",
        "yahoo",
        "baidu",
        "duckduckgo",
        "searx",
        "searxng",
    }
)


def _host(url: str) -> str:
    hostname = (urllib.parse.urlsplit(url).hostname or "").rstrip(".").lower()
    try:
        return hostname.encode("idna").decode("ascii")
    except UnicodeError:
        return hostname


def _matches_domain(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def excluded_site_reason(url: str) -> str | None:
    """Return why a URL must not be crawled, or ``None`` when it is allowed."""
    try:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
            return "only HTTP(S) sites can be crawled"
        parts.port  # reject malformed ports before a URL reaches an HTTP client
    except ValueError:
        return "invalid HTTP(S) URL"

    host = _host(url)
    if any(_matches_domain(host, domain) for domain in WIKI_DOMAINS):
        return "wiki/reference sites are excluded"
    if any(_matches_domain(host, domain) for domain in VIDEO_DOMAINS):
        return "YouTube/video-hosting pages are excluded"
    if any(_matches_domain(host, domain) for domain in ARCHIVE_DOMAINS):
        return "Internet Archive pages are excluded"
    if any(_matches_domain(host, domain) for domain in APP_DOMAINS):
        return "app/software download sites are excluded"
    if any(_matches_domain(host, domain) for domain in TELEGRAM_DOMAINS):
        return "Telegram messaging pages are excluded"
    if any(_matches_domain(host, domain) for domain in GITHUB_DOMAINS):
        return "GitHub-hosted code and pages are excluded"
    if any(_matches_domain(host, domain) for domain in SEARCH_DOMAINS) or any(
        label in SEARCH_ENGINE_LABELS for label in host.split(".")
    ):
        return "search-engine results and redirectors are excluded"
    return None
