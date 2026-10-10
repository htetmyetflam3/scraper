#!/usr/bin/env python3
"""Search-engine book-link crawler.

Finds Burmese PDF/DOCX files and Scribd documents by paging through search
engine results. Results are read from the DOM with CSS selectors, so no
hand-written HTML parsing: `--fetcher=chromium` runs a real headless Chromium
when an engine refuses plain HTTP requests. It drives the system browser
(`chromium --headless --dump-dom`), so there is no Python browser package to
install -- which matters on platforms where the Playwright wheel does not
exist, such as Alpine/musl on aarch64.

    python search_crawl.py                       # Mojeek, default terms
    python search_crawl.py --engine=searx
    python crawler.py --fetcher=chromium    # headless Chromium (system binary)
    python search_crawl.py --reset-state         # re-read everything

State lives in search_crawled_pages.txt / search_pending_pages.txt next to this
file; found links go to search_entry_list.txt and search_scribd_links.txt as
"Book Name<TAB>URL<TAB>Source Page" rows.
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
from contextlib import ExitStack
from pathlib import Path

try:
    import requests
    from bs4 import BeautifulSoup
except ModuleNotFoundError:  # pragma: no cover
    sys.exit("Missing dependencies. Run:  uv sync  (or pip install requests beautifulsoup4)")

HERE = Path(__file__).resolve().parent

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

DEFAULT_TERMS = [
    "Burmese book PDF download link",
    "Burmese PDF",
    "Myanmar ဝတ္ထု",
    "မြန်မာစာ",
    "ဝတ္ထု",
    "ရသ",
    "ကဗျာများ free download",
    "မြန်မာစာပေ",
    "မြန်မာဝတ္တု",
    "မြန်မာဝတ္ထု",
    "သုတစာပေ",
    "ရသစာပေ",
    "အချစ်ဝတ္ထု",
    "စိတ်ကူးယဉ်ဝတ္ထု",
    "နာမည်ကြီးစာရေးဆရာများ၏ PDF download linkများ",
    "နာမည်ကြီးစာရေးဆရာများ",
    "မြန်မာစာအုပ် PDF download",
    "မြန်မာကဗျာများ free download",
    "Myanmar book PDF free download",
    "Burmese novel PDF download",
    "မြန်မာစာအုပ်များ download",
    "Myanmar",
    "Burmese",
    "Burma",
    "မြန်မာ",
    "Myanmar PDF free download",
    "Burma book PDF download",
    "Burmese book free download",
]

MYANMAR_SCRIPT = re.compile(r"[က-၁]")
LATIN_INTEREST = re.compile(r"myanmar|burmese|burma", re.I)
BOOK_EXTENSION = re.compile(r"\.(pdf|docx)$", re.I)
SCRIBD_DOCUMENT = re.compile(r"^/(?:doc|document|book)/\d+(?:/|$)", re.I)

BLOCKED_PATTERNS = [
    re.compile(p, re.I)
    for p in (
        r"just a moment",
        r"attention required",
        r"checking your browser",
        r"enable cookies and reload",
        r"before you continue to google",
        r"\bcaptcha\b",
        r"unusual traffic",
        r"verify (?:that )?you(?:'re| are) (?:a )?human",
        r"our systems have detected",
        r"please enable javascript",
        r"enable javascript to continue",
        r"javascript is (?:disabled|turned off)",
        r"sign in to confirm",
        r"temporarily (?:unavailable|blocked)",
        r"access denied",
        # Mojeek's ALTCHA challenge: the title says "Captcha" but the body says
        # "Verification required" and never necessarily uses the word captcha.
        r"\baltcha\b",
        r"verification required",
        r"please complete the challenge",
        r"waiting for verification",
        # Mojeek's 403 page when it decides the client is a bot. Without this the
        # page was classified "empty" and the crawler kept requesting.
        r"appears to be sending automated queries",
        r"automated queries",
        r"cannot process your search at this time",
        r"can't process your search at this time",
        r"403 - forbidden",
    )
]

NO_RESULTS_PATTERNS = [
    re.compile(p, re.I)
    for p in (
        r"\bb_no\b",
        r"no results (?:were )?found",
        r"did not match any",
        r"we did not find any results",
        r"couldn't find any results",
        r"your search did not",
        r"there are no results for",
    )
]

# Statuses an engine uses to say "stop": 429/503 are a throttle, 401/403 a refusal.
RATE_LIMIT_STATUSES = {429, 503}
BLOCKED_STATUSES = {401, 403}


class RateLimited(Exception):
    def __init__(self, message: str, retry_after: float = 0.0):
        super().__init__(message)
        self.retry_after = retry_after


class Blocked(Exception):
    pass


# --------------------------------------------------------------------------- #
# Engines
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class Engine:
    name: str
    base_url: str
    result_selector: str
    link_selector: str
    title_selector: str
    per_page: int = 10

    def page_params(self, page: int) -> dict[str, str]:
        """Query parameters for a zero-based result page."""
        params = {"q": "{query}"}
        if self.name == "mojeek":
            # Mojeek rate-limits requests that set s=0, so page one omits it.
            # s is a 1-based result index: page two starts at result 11.
            if page > 0:
                params["s"] = str(page * self.per_page + 1)
        elif self.name == "searx":
            params.update(pageno=str(page + 1), categories="general", safesearch="0", language="all")
        elif self.name == "brave":
            # offset is a zero-based *page* index; spellcheck=0 keeps Burmese terms.
            if page > 0:
                params["offset"] = str(page)
            params["spellcheck"] = "0"
        elif self.name == "bing":
            params.update(count=str(self.per_page), first=str(page * self.per_page + 1))
        elif self.name == "duckduckgo":
            params["s"] = str(page * self.per_page)
        else:  # google
            params.update(num=str(self.per_page), hl="en", start=str(page * self.per_page))
        return params

    def page_url(self, query: str, page: int) -> str:
        params = {key: (value if value != "{query}" else query) for key, value in self.page_params(page).items()}
        return f"{self.base_url}?{urllib.parse.urlencode(params)}"

    @property
    def host(self) -> str:
        return urllib.parse.urlparse(self.base_url).netloc.lower()


ENGINES = {
    "mojeek": Engine("mojeek", "https://www.mojeek.com/search", 'ul[class*="results"] li', "h2 a", "h2"),
    "searx": Engine("searx", "https://searx.be/search", "article.result, div.result", "h3 a", "h3"),
    "brave": Engine("brave", "https://search.brave.com/search", 'div.snippet[data-type="web"]', "a[href]", "div.title", 20),
    "bing": Engine("bing", "https://www.bing.com/search", "li.b_algo, div.b_algo", "h2 a", "h2"),
    "google": Engine("google", "https://www.google.com/search", "div.MjjYud, div.g, div.tF2Cxc", "a[href]", "h3"),
    "duckduckgo": Engine("duckduckgo", "https://html.duckduckgo.com/html/", "div.result, div.web-result", "a.result__a", "a.result__a", 30),
}
BOT_FRIENDLY = ("mojeek", "searx")


def build_queries(terms: list[str]) -> list[str]:
    queries: list[str] = []
    for term in terms:
        queries.extend([term, f"{term} filetype:pdf", f"{term} filetype:docx", f"site:scribd.com {term}"])
    return list(dict.fromkeys(queries))


# --------------------------------------------------------------------------- #
# URLs
# --------------------------------------------------------------------------- #


def base_domain(host: str) -> str:
    labels = host.lower().split(".")
    return ".".join(labels[-2:]) if len(labels) > 2 else ".".join(labels)


def decode_bing_redirect(value: str) -> str | None:
    raw = value[2:] if value.startswith("a1") else value
    try:
        decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf8", "replace")
    except Exception:
        return None
    return decoded if decoded.lower().startswith(("http://", "https://")) else None


def unwrap_result_url(href: str, page_url: str) -> str | None:
    """Resolve the engine's redirect wrappers to the real result URL."""
    if not href or href.startswith("#"):
        return None
    url = urllib.parse.urljoin(page_url, href)
    for _ in range(3):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https"):
            return None
        query = urllib.parse.parse_qs(parts.query)
        host = parts.netloc.lower()
        target = None
        if "duckduckgo.com" in host or (parts.path.endswith("/l/") and "uddg" in query):
            target = query.get("uddg", [None])[0]
        elif host == "google.com" or host.endswith(".google.com") or parts.path == "/url":
            target = query.get("url", query.get("q", [None]))[0]
        elif (host == "bing.com" or host.endswith(".bing.com")) and "u" in query:
            target = decode_bing_redirect(query["u"][0])
        if not target:
            break
        url = urllib.parse.urljoin(url, target)
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return None
    return normalize_url(urllib.parse.urlunsplit(parts._replace(fragment="")))


def normalize_url(url: str) -> str:
    """Percent-encode non-ASCII characters so URLs compare and store predictably."""
    return urllib.parse.quote(url, safe=":/?#[]@!$&'()*+,;=~%")


def is_scribd_document_url(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    host = parts.netloc.lower().removeprefix("www.")
    if host != "scribd.com" and not host.endswith(".scribd.com"):
        return False
    return bool(SCRIBD_DOCUMENT.match(parts.path))


def looks_burmese_related(text: str) -> bool:
    return bool(LATIN_INTEREST.search(text or "") or MYANMAR_SCRIPT.search(text or ""))


def fallback_book_name(url: str) -> str:
    name = urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1])
    return BOOK_EXTENSION.sub("", name) or url


def matches_book_candidate(url: str, title: str, mode: str = "loose") -> bool:
    if not BOOK_EXTENSION.search(urllib.parse.urlsplit(url).path.lower()):
        return False
    if mode == "filename":
        return looks_burmese_related(urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]))
    return (
        looks_burmese_related(urllib.parse.unquote(urllib.parse.urlsplit(url).path))
        or looks_burmese_related(title)
        or looks_burmese_related(url)
    )


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def extract_results(html: str, page_url: str, engine: Engine) -> tuple[list[dict], bool]:
    """Pull organic results out of a result page.

    Returns (results, used_fallback). Results are read with CSS selectors on the
    DOM; if the engine's markup changes, the fallback takes every external link
    on the page so a run degrades into "extra candidates" instead of "nothing".
    """
    soup = BeautifulSoup(html, "html.parser")
    engine_host = base_domain(urllib.parse.urlsplit(page_url).netloc or engine.host)
    seen: set[str] = set()
    results: list[dict] = []
    used_fallback = False

    containers = soup.select(engine.result_selector)
    if not containers:
        used_fallback = True
        containers = (
            soup.select("a[href]")
            if not any(pattern.search(html) for pattern in NO_RESULTS_PATTERNS)
            else []
        )

    for container in containers:
        link = container.select_one(engine.link_selector) or container.select_one("a[href]") or container
        if link.name == "a" and not link.get("href"):
            continue
        href = link.get("href") if link.name == "a" else (container.get("href") if container.name == "a" else None)
        if not href:
            anchor = container.find("a", href=True)
            if not anchor:
                continue
            href = anchor["href"]
            link = anchor
        url = unwrap_result_url(href, page_url)
        if not url or url in seen:
            continue
        if base_domain(urllib.parse.urlsplit(url).netloc) == engine_host:
            continue
        title_node = container.select_one(engine.title_selector)
        title = title_node.get_text(" ", strip=True) if title_node else link.get_text(" ", strip=True)
        seen.add(url)
        results.append({"url": url, "title": re.sub(r"\s+", " ", title or "").strip()})

    return results, used_fallback


def classify_page(html: str, result_count: int) -> str:
    if result_count > 0:
        return "results"
    if any(pattern.search(html) for pattern in BLOCKED_PATTERNS):
        return "blocked"
    if any(pattern.search(html) for pattern in NO_RESULTS_PATTERNS):
        return "no-results"
    return "empty"


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #


class HttpFetcher:
    """requests.Session fetcher: keeps cookies, backs off on 429/503."""

    def __init__(self, timeout: float = 60.0, retries: int = 3, backoff: float = 5.0, max_backoff: float = 120.0, user_agent: str = USER_AGENT):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.max_backoff = max_backoff

    def close(self) -> None:
        self.session.close()

    def fetch(self, url: str) -> tuple[str, str]:
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                response = self.session.get(url, timeout=self.timeout)
            except requests.RequestException as error:
                # A dropped connection is not a throttle: retry quickly, and save
                # the long backoff for 429/503.
                last_error = error
                if attempt < self.retries:
                    time.sleep(min(2 * attempt, 10))
                continue

            if response.status_code in RATE_LIMIT_STATUSES:
                wait = parse_retry_after(response.headers.get("Retry-After"))
                wait = min(max(wait, self.backoff * 2 ** (attempt - 1)), self.max_backoff)
                if attempt < self.retries:
                    print(f"  Rate limited (HTTP {response.status_code}); waiting {wait:.0f}s.")
                    time.sleep(wait)
                    continue
                raise RateLimited(f"HTTP {response.status_code}", wait)

            if response.status_code in BLOCKED_STATUSES:
                raise Blocked(f"HTTP {response.status_code} {response.reason}")

            if not response.ok:
                raise RuntimeError(f"HTTP {response.status_code} {response.reason}")

            content_type = response.headers.get("Content-Type", "").lower()
            if content_type and "html" not in content_type and "xhtml" not in content_type:
                raise RuntimeError(f"not an HTML response ({content_type})")
            if "charset" not in content_type:
                # Many sites send no charset; requests then decodes as Latin-1 and Burmese names turn to mojibake.
                response.encoding = "utf-8"
            return response.text, response.url

        raise last_error or RuntimeError("request failed")


class BrowserFetcher:
    """Headless Chromium via Playwright.

    A real browser executes JavaScript, keeps cookies and looks like a browser,
    which is what engines that answer HTTP with 403 require.
    """

    def __init__(self, wait_selector: str | None = None, timeout_ms: int = 45_000, headless: bool = True, user_agent: str = USER_AGENT):
        try:
            from playwright.sync_api import sync_playwright  # noqa: PLC0415
        except ModuleNotFoundError as error:  # pragma: no cover
            raise RuntimeError(
                "Playwright is not installed (it ships no wheel for musl/Alpine, and its "
                "bundled Chromium needs glibc). Install the system Chromium instead: "
                "  apk add chromium   /   pacman -S chromium   /   apt install chromium  "
                "-- the crawler uses it automatically."
            ) from error
        self._sync_playwright = sync_playwright
        self.wait_selector = wait_selector
        self.timeout_ms = timeout_ms
        self.headless = headless
        self.user_agent = user_agent
        self._stack = None
        self._page = None

    def __enter__(self) -> "BrowserFetcher":
        self._stack = self._sync_playwright().start()
        try:
            browser = self._stack.chromium.launch(headless=self.headless)
        except Exception as error:  # pragma: no cover - depends on installed browsers
            self._stack.stop()
            raise RuntimeError(
                "Could not launch Chromium. Run:  uv run playwright install chromium  "
                f"({error})"
            ) from error
        context = browser.new_context(user_agent=self.user_agent, locale="en-US", viewport={"width": 1366, "height": 900})
        self._page = context.new_page()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        if self._stack:
            self._stack.stop()
            self._stack = None
            self._page = None

    def fetch(self, url: str) -> tuple[str, str]:
        if self._page is None:
            raise RuntimeError("browser is not running (BrowserFetcher used without entering its context)")
        response = self._page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        try:
            if self.wait_selector:
                self._page.wait_for_selector(self.wait_selector, timeout=self.timeout_ms)
            else:
                self._page.wait_for_load_state("networkidle", timeout=min(self.timeout_ms, 15_000))
        except Exception:
            pass  # Rendered HTML is still usable if the selector never appears.
        return self._page.content(), self._page.url or url


# Chromium binaries to look for, in order. Alpine ships "chromium", Arch/Debian
# ship "chromium" too, Google's builds are "google-chrome*".
CHROMIUM_NAMES = (
    "chromium",
    "chromium-browser",
    "chromium-headless-shell",
    "google-chrome",
    "google-chrome-stable",
    "chrome",
)


def find_chromium(explicit: str | None = None) -> list[str] | None:
    """Locate a Chromium binary: --browser-path, $CHROMIUM_PATH, then $PATH.

    Returns an argv prefix. --browser-path may be a whole command, which is how
    you point at a browser inside a container, e.g.

        --browser-path="docker run --rm --entrypoint chromium IMAGE"
    """
    if explicit:
        argv = shlex.split(explicit)
        if not argv:
            return None
        head = Path(argv[0]).expanduser()
        if head.exists():
            return [str(head)] + argv[1:]
        if shutil.which(argv[0]):
            return [shutil.which(argv[0])] + argv[1:]
        return None
    env = os.environ.get("CHROMIUM_PATH")
    if env:
        head = Path(env).expanduser()
        if head.exists():
            return [str(head)]
    for name in CHROMIUM_NAMES:
        found = shutil.which(name)
        if found:
            return [found]
    return None


class ChromiumFetcher:
    """Headless Chromium driven through the system binary, with no Python package.

    `chromium --headless --dump-dom` renders the page (JavaScript included) and
    prints the DOM to stdout, which is all the parser needs. It needs nothing
    installed beyond the browser itself, so it works on platforms where the
    Playwright wheel is unavailable -- Alpine/musl on aarch64, for instance,
    where Playwright ships no wheel and its bundled Chromium would not run
    anyway because that build is linked against glibc.

    One browser per page is slower than a persistent browser, so a --user-data-dir
    keeps cookies between pages.
    """

    def __init__(
        self,
        binary: str | None = None,
        timeout: float = 60.0,
        user_agent: str = USER_AGENT,
        virtual_time_budget_ms: int = 5_000,
        no_sandbox: bool = True,
    ):
        self.argv = find_chromium(binary)
        if not self.argv:
            raise RuntimeError(
                "No Chromium binary found. Install one, e.g.  apk add chromium  "
                "(Alpine),  pacman -S chromium  (Arch),  apt install chromium  "
                "(Debian), or point at one with --browser-path=/path/to/chromium."
            )
        self.timeout = timeout
        self.user_agent = user_agent
        self.virtual_time_budget_ms = virtual_time_budget_ms
        self.no_sandbox = no_sandbox
        self._profile = None

    def __enter__(self) -> "ChromiumFetcher":
        self._profile = tempfile.mkdtemp(prefix="search-crawl-chromium-")
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        if self._profile:
            shutil.rmtree(self._profile, ignore_errors=True)
            self._profile = None

    def command(self, url: str) -> list[str]:
        cmd = list(self.argv) + [
            "--headless=new",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--hide-scrollbars",
            "--no-first-run",
            f"--user-agent={self.user_agent}",
            f"--virtual-time-budget={self.virtual_time_budget_ms}",
            f"--user-data-dir={self._profile}",
        ]
        if self.no_sandbox:
            cmd.append("--no-sandbox")  # required when running as root, e.g. in a container
        cmd += ["--dump-dom", url]
        return cmd

    def fetch(self, url: str) -> tuple[str, str]:
        if self._profile is None:  # pragma: no cover - used as a context manager
            raise RuntimeError("ChromiumFetcher used without entering its context")
        try:
            proc = subprocess.run(
                self.command(url),
                capture_output=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(f"Chromium timed out after {self.timeout:.0f}s") from error
        except OSError as error:  # pragma: no cover - binary missing/not executable
            raise RuntimeError(f"Could not run {' '.join(self.argv)}: {error}") from error
        html = proc.stdout.decode("utf-8", "replace")
        if proc.returncode != 0 and not html.strip():
            detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
            raise RuntimeError(f"Chromium exited {proc.returncode}: {detail[-1] if detail else 'no output'}")
        return html, url


def parse_retry_after(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        return 0.0


# --------------------------------------------------------------------------- #
# State files
# --------------------------------------------------------------------------- #


def read_url_list(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line.strip() for line in path.read_text(encoding="utf8").splitlines() if line.strip() and not line.startswith("#")]


def write_url_list(path: Path, urls) -> None:
    unique = sorted(set(urls))
    path.write_text("".join(f"{url}\n" for url in unique), encoding="utf8")


def clean_cell(value: str) -> str:
    return re.sub(r"[\t\r\n]+", " ", str(value or "")).strip()


def read_book_list(path: Path) -> dict[str, dict]:
    records: dict[str, dict] = {}
    if not path.exists():
        return records
    lines = [line for line in path.read_text(encoding="utf8").splitlines() if line.strip()]
    if not lines:
        return records
    header = [cell.strip().lower() for cell in lines[0].split("\t")]
    if "url" not in header or "book name" not in header:
        return records
    url_index, name_index = header.index("url"), header.index("book name")
    source_index = header.index("source page") if "source page" in header else None
    for line in lines[1:]:
        columns = line.split("\t")
        url = columns[url_index].strip()
        if not url:
            continue
        source = columns[source_index].strip() if source_index is not None and source_index < len(columns) else ""
        records[url] = {"name": clean_cell(columns[name_index]), "url": url, "source": clean_cell(source)}
    return records


def write_book_list(path: Path, records: dict[str, dict]) -> None:
    rows = sorted(records.values(), key=lambda record: record["url"])
    lines = ["Book Name\tURL\tSource Page"]
    lines += [f'{clean_cell(row["name"])}\t{row["url"]}\t{clean_cell(row["source"])}' for row in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf8")


def is_weak_name(name: str, url: str) -> bool:
    value = (name or "").strip()
    return not value or value == fallback_book_name(url) or "://" in value or "›" in value


def upsert(records: dict[str, dict], url: str, title: str, source: str) -> None:
    name = clean_cell(title) or fallback_book_name(url)
    previous = records.get(url)
    if previous is None or (not is_weak_name(name, url) and is_weak_name(previous["name"], url)):
        records[url] = {"name": name, "url": url, "source": clean_cell(source)}


def save_debug_html(directory: Path | None, url: str, html: str, index: int, limit: int = 20) -> Path | None:
    if not directory or index >= limit:
        return None
    directory.mkdir(parents=True, exist_ok=True)
    label = re.sub(r"[^a-z\d]+", "_", url, flags=re.I)[-80:] or "page"
    path = directory / f"{index:02d}-{label}.html"
    path.write_text(html, encoding="utf8")
    return path


# --------------------------------------------------------------------------- #
# Sites to crawl (handed to dsite.py)
# --------------------------------------------------------------------------- #


def site_host(url: str) -> str:
    return urllib.parse.urlsplit(url).netloc.lower().removeprefix("www.")


def is_site_seed(url: str) -> bool:
    """A search result page on an ordinary website: worth crawling for PDFs.

    Scribd documents, direct files and the search engines' own pages are not.
    """
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return False
    if is_scribd_document_url(url) or BOOK_EXTENSION.search(parts.path.lower()):
        return False
    engine_hosts = {site_host(engine.base_url) for engine in ENGINES.values()}
    host = site_host(url)
    return not any(host == name or host.endswith("." + name) for name in engine_hosts)


def add_site_seed(sites: dict[str, str], url: str) -> bool:
    """One seed per domain: the first result page seen from that domain. True if added."""
    host = site_host(url)
    if host in sites:
        return False
    sites[host] = url
    return True


# --------------------------------------------------------------------------- #
# Crawl
# --------------------------------------------------------------------------- #


def crawl(args: argparse.Namespace) -> int:
    engine = ENGINES[args.engine]
    if args.base_url:
        engine = dataclasses.replace(engine, base_url=args.base_url.rstrip("?"))

    queries = args.query or ([t.strip() for t in args.terms.split(",") if t.strip()] and build_queries([t.strip() for t in args.terms.split(",") if t.strip()]))
    queries = list(dict.fromkeys(q for q in queries if q))
    if not queries:
        print("No search queries configured.", file=sys.stderr)
        return 1

    generated = [engine.page_url(query, page) for query in queries for page in range(args.pages)]
    completed = set() if args.reset_state else set(read_url_list(args.crawled))
    pending = (
        []
        if args.reset_state
        else [url for url in dict.fromkeys(read_url_list(args.pending)) if same_engine(url, engine) and url not in completed]
    )
    known = set(pending)
    pending += [url for url in generated if url not in completed and url not in known]

    file_links = read_book_list(args.entry_list)
    scribd_links = read_book_list(args.scribd_list)
    site_seeds: dict[str, str] = {}
    for url in read_url_list(args.sites):
        add_site_seed(site_seeds, url)
    sites_before = len(site_seeds)

    print(f"Search engine: {engine.name} ({engine.base_url})")
    print(f"Fetcher: {args.fetcher}")
    print(f"Queries: {len(queries)}; pages per query: {args.pages}.")
    print(f"Book match mode: {args.match_mode}.")

    stats = {key: 0 for key in ("with_results", "no_results", "blocked", "unparsed", "rate_limited", "fetch_failed", "skipped")}
    delay = args.delay
    consecutive_blocked = consecutive_rate_limited = consecutive_empty = 0
    debug_index = 0
    fetcher: HttpFetcher | BrowserFetcher

    with ExitStack() as stack:
        fetcher = make_fetcher(args, engine, stack)
        write_url_list(args.pending, pending)

        index = 0
        while index < len(pending):
            page_url = pending[index]
            if page_url in completed:
                index += 1
                continue
            print(f"Search page {index + 1}/{len(pending)}: {page_url}")

            try:
                html, final_url = fetcher.fetch(page_url)
            except RateLimited as error:
                stats["rate_limited"] += 1
                consecutive_rate_limited += 1
                delay = min(max(delay * 2, error.retry_after), args.max_delay)
                print(f"  Rate limited: {error}. Slowing to {delay:.0f}s between requests.")
                if consecutive_rate_limited >= args.max_rate_limited:
                    print(
                        f"\n{consecutive_rate_limited} consecutive rate-limited pages: {engine.name} is throttling "
                        "this client. Stopping; the remaining pages stay queued. Wait a while, raise --delay, or "
                        f"switch --engine ({' or '.join(n for n in BOT_FRIENDLY if n != engine.name)})."
                    )
                    break
                index += 1
                time.sleep(delay)
                continue
            except Blocked as error:
                stats["blocked"] += 1
                consecutive_blocked += 1
                print(f"  Refused: {error}")
                if consecutive_blocked >= args.max_blocked:
                    print(
                        f"\n{consecutive_blocked} consecutive refused pages: {engine.name} is refusing requests. "
                        "Stopping; the remaining pages stay queued. Try --fetcher=chromium (a real browser looks "
                        "nothing like this client), switch --engine, or set --user-agent."
                    )
                    break
                index += 1
                time.sleep(delay)
                continue
            except Exception as error:
                stats["fetch_failed"] += 1
                consecutive_blocked = consecutive_rate_limited = 0
                print(f"  Failed: {str(error) or type(error).__name__}")
                index += 1
                time.sleep(delay)
                continue

            results, used_fallback = extract_results(html, final_url, engine)
            status = classify_page(html, len(results))

            new_files = new_scribd = rejected = 0
            for result in results:
                if is_scribd_document_url(result["url"]):
                    if result["url"] not in scribd_links:
                        new_scribd += 1
                    upsert(scribd_links, result["url"], result["title"], final_url)
                elif matches_book_candidate(result["url"], result["title"], args.match_mode):
                    if result["url"] not in file_links:
                        new_files += 1
                    upsert(file_links, result["url"], result["title"], final_url)
                elif BOOK_EXTENSION.search(result["url"].lower()):
                    rejected += 1
                elif is_site_seed(result["url"]):
                    add_site_seed(site_seeds, result["url"])

            if len(site_seeds) != sites_before:
                write_url_list(args.sites, site_seeds.values())
                sites_before = len(site_seeds)

            detail = f"results: {len(results)}; new files: {new_files}; new Scribd: {new_scribd}"
            if rejected:
                detail += f"; PDF/DOCX skipped as unrelated: {rejected}"
            print(f"  {detail}.")
            # Show what the engine returned that we did not keep, so an empty
            # "new files" line can be told apart from a filter that is too strict.
            dropped = [
                r for r in results
                if not is_scribd_document_url(r["url"]) and not matches_book_candidate(r["url"], r["title"], args.match_mode)
            ]
            for item in dropped[:5]:
                print(f"    not kept: {item['title'][:70]!r} -> {item['url'][:140]}")
            if len(dropped) > 5:
                print(f"    ... and {len(dropped) - 5} more not kept")

            if status == "results":
                stats["with_results"] += 1
                if used_fallback:
                    print("  (read with the generic link fallback: the engine's markup may have changed)")
                consecutive_blocked = consecutive_rate_limited = consecutive_empty = 0
            elif status == "no-results":
                stats["no_results"] += 1
                consecutive_blocked = consecutive_rate_limited = consecutive_empty = 0
                print("  Engine reported no results for this query page.")
            else:
                stats["unparsed" if status == "empty" else "blocked"] += 1
                path = save_debug_html(args.debug_dir, final_url, html, debug_index)
                debug_index += 1
                print(
                    f"  No results could be read ({status}); page left pending for retry."
                    + (f" HTML saved to {path}" if path else "")
                )
                if status == "empty":
                    consecutive_empty += 1
                    if consecutive_empty >= args.max_blocked:
                        print(
                            f"\n{consecutive_empty} consecutive pages came back with no results, no "
                            "'no results' message and no block marker. The engine's markup or its response "
                            f"has changed. Stopping. Look at the saved HTML in {args.debug_dir}."
                        )
                        break
                if status == "blocked":
                    consecutive_blocked += 1
                    if consecutive_blocked >= args.max_blocked:
                        print(
                            f"\n{consecutive_blocked} consecutive blocked pages: {engine.name} is refusing "
                            "requests. Stopping; the remaining pages stay queued. Try --fetcher=chromium."
                        )
                        break
                index += 1
                time.sleep(delay)
                continue

            completed.add(page_url)
            completed.add(final_url)
            index += 1
            write_url_list(args.crawled, completed)
            write_url_list(args.pending, [url for url in pending[index:] if url not in completed])
            write_book_list(args.entry_list, file_links)
            write_book_list(args.scribd_list, scribd_links)
            time.sleep(delay + random.uniform(0, args.jitter))

    write_book_list(args.entry_list, file_links)
    write_book_list(args.scribd_list, scribd_links)
    write_url_list(args.crawled, completed)
    write_url_list(args.pending, [url for url in pending if url not in completed])

    print("\nSummary:")
    for label, key in (
        ("pages with results", "with_results"),
        ("pages the engine said were empty", "no_results"),
        ("pages blocked by the engine", "blocked"),
        ("pages with unreadable HTML", "unparsed"),
        ("pages rate limited (HTTP 429)", "rate_limited"),
        ("pages that failed to download", "fetch_failed"),
    ):
        print(f"  {label + ':':38s} {stats[key]}")
    print(f"Matching PDF/DOCX links: {len(file_links)} ({args.entry_list})")
    print(f"Scribd document links: {len(scribd_links)} ({args.scribd_list})")
    print(f"Completed search pages: {len(completed)} ({args.crawled})")
    return 0


def same_engine(url: str, engine: Engine) -> bool:
    return base_domain(urllib.parse.urlsplit(url).netloc) == base_domain(engine.host)


def make_fetcher(args: argparse.Namespace, engine: Engine, stack: ExitStack):
    """Return a fetcher registered with `stack`, falling back to HTTP when a browser is absent."""
    notes: list[str] = []

    if args.fetcher in ("auto", "chromium"):
        try:
            fetcher = ChromiumFetcher(binary=args.browser_path, user_agent=args.user_agent)
            stack.enter_context(fetcher)
            return fetcher
        except RuntimeError as error:
            if args.fetcher == "chromium":
                raise
            notes.append(str(error))

    if args.fetcher in ("auto", "playwright", "browser"):
        try:
            browser = BrowserFetcher(
                wait_selector=engine.result_selector,
                headless=not args.headful,
                user_agent=args.user_agent,
            )
            stack.enter_context(browser)
            return browser
        except RuntimeError as error:
            if args.fetcher in ("playwright", "browser"):
                raise
            notes.append(str(error))

    if args.fetcher != "http":
        print("No headless browser available:")
        for note in notes or ["none found"]:
            print(f"  - {note}")
        print("Falling back to plain HTTP requests.")

    fetcher = HttpFetcher(user_agent=args.user_agent)
    stack.callback(fetcher.close)
    return fetcher


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Search-engine book-link crawler")
    parser.add_argument("query", nargs="*", help="exact queries (default: generated from --terms)")
    parser.add_argument("--engine", default="mojeek", choices=sorted(ENGINES))
    parser.add_argument("--base-url", default="", help="override the engine URL, e.g. your own SearXNG instance")
    parser.add_argument("--fetcher", default="auto", choices=("auto", "http", "chromium", "playwright", "browser"),
                        help="http = requests; chromium = system Chromium (--headless --dump-dom, no Python package "
                             "needed); playwright = Playwright's bundled Chromium; auto = whichever is available")
    parser.add_argument("--browser-path", default=None, metavar="PATH",
                        help="path to a Chromium/Chrome binary (else $CHROMIUM_PATH, then $PATH)")
    parser.add_argument("--headful", action="store_true", help="show the browser window (debug; Playwright only)")
    parser.add_argument("--delay", type=float, default=2.0, help="seconds between requests (default 2)")
    parser.add_argument("--jitter", type=float, default=0.5, help="random extra delay, seconds (default 0.5)")
    parser.add_argument("--pages", type=int, default=10, help="result pages per query (default 10)")
    parser.add_argument("--terms", default=",".join(DEFAULT_TERMS), help="comma-separated search terms")
    parser.add_argument("--match-mode", default="loose", choices=("loose", "filename"))
    parser.add_argument("--max-blocked", type=int, default=5)
    parser.add_argument("--max-rate-limited", type=int, default=3)
    parser.add_argument("--max-delay", type=float, default=60.0)
    parser.add_argument("--reset-state", action="store_true", help="clear the page history and scan everything again")
    parser.add_argument("--debug-dir", type=Path, default=HERE / "debug_html",
                        help="save HTML of pages that could not be read (first 20 per run)")
    parser.add_argument("--user-agent", default=USER_AGENT, help="override the User-Agent header")
    parser.add_argument("--entry-list", type=Path, default=HERE / "search_entry_list.txt")
    parser.add_argument("--scribd-list", type=Path, default=HERE / "search_scribd_links.txt")
    parser.add_argument("--sites", type=Path, default=HERE / "search_sites.txt",
                        help="ordinary websites found by search, for dsite.py")
    parser.add_argument("--crawled", type=Path, default=HERE / "search_crawled_pages.txt")
    parser.add_argument("--pending", type=Path, default=HERE / "search_pending_pages.txt")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return crawl(args)
    except KeyboardInterrupt:
        print("\nInterrupted; progress is saved.", file=sys.stderr)
        return 130
    except RuntimeError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
