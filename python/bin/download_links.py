"""Static PDF/DOCX link extraction and guards; no page traversal."""

from __future__ import annotations

import re
import urllib.parse

from bs4 import BeautifulSoup

FILE_EXTENSION = re.compile(r"\.(pdf|docx)$", re.I)
NON_DOWNLOAD_QUERY_KEYS = frozenset(
    {
        "share",
        "shared",
        "sharing",
        "share_source",
        "replytocom",
        "amp",
        "output",
        "print",
    }
)
TRACKING_QUERY_KEYS = frozenset({"fbclid", "gclid", "dclid", "msclkid", "yclid"})


def guard_download_url(url: str) -> str | None:
    """Reject share/comment/print variants and strip fragments and tracking keys.

    Scheme and host are lowercased (IDNA-encoded) so textual variants of one
    URL share a single identity; path case, userinfo, port and kept queries
    are preserved. Passing this guard does not prove that a response
    contains a file. The file-link extractor checks the path; PDF bytes are
    checked separately.
    """
    try:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
            return None
        host = (parts.hostname or "").lower()
        port = parts.port  # reject malformed ports before a link reaches an HTTP client
    except ValueError:
        return None
    if not host:
        return None
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    if any(key.lower() in NON_DOWNLOAD_QUERY_KEYS for key, _ in query):
        return None
    kept = [
        (key, value)
        for key, value in query
        if key.lower() not in TRACKING_QUERY_KEYS and not key.lower().startswith("utm_")
    ]
    cleaned_query = (
        parts.query
        if len(kept) == len(query)
        else urllib.parse.urlencode(kept, doseq=True)
    )
    try:
        idna_host = host.encode("idna").decode("ascii")
    except UnicodeError:
        idna_host = host
    if ":" in idna_host and not idna_host.startswith("["):
        idna_host = f"[{idna_host}]"
    netloc = idna_host
    if "@" in parts.netloc:
        netloc = parts.netloc.rsplit("@", 1)[0] + "@" + netloc
    if port is not None:
        netloc = f"{netloc}:{port}"
    return urllib.parse.urlunsplit(
        (parts.scheme.lower(), netloc, parts.path or "/", cleaned_query, "")
    )


FILE_IN_CODE = re.compile(
    r"""['"]([^'"\s<>]+?\.(?:pdf|docx)(?:\?[^'"\s<>]*)?)['"]""", re.I
)


def extract_file_links(html: str, page_url: str) -> list[tuple[str, str]]:
    """Return (absolute PDF/DOCX URL, visible label) from anchors and controls.

    Handles <a href>, form/action attributes, data-* attributes and quoted file
    paths in onclick. A button that names no file URL contributes nothing.
    Cross-host file links are allowed; this function never follows HTML pages.
    """
    soup = BeautifulSoup(html, "html.parser")
    base_tag = soup.find("base", href=True)
    base = urllib.parse.urljoin(page_url, base_tag["href"]) if base_tag else page_url
    links: dict[str, str] = {}

    def add(href: str, label: str) -> None:
        href = href.strip()
        if not href or href.startswith("#"):
            return
        url = guard_download_url(urllib.parse.urljoin(base, href))
        if url is not None and FILE_EXTENSION.search(urllib.parse.urlsplit(url).path):
            links.setdefault(url, label)

    for anchor in soup.find_all("a", href=True):
        label = anchor.get_text(" ", strip=True) or anchor.get("title", "") or ""
        add(anchor["href"], " ".join(label.split()))

    for element in soup.find_all(True):
        label = (
            " ".join(element.get_text(" ", strip=True).split())[:200]
            or element.get("title", "")
            or ""
        )
        for name, value in element.attrs.items():
            if not isinstance(value, str) or element.name == "a" and name == "href":
                continue
            if name in ("formaction", "action") or name.startswith("data-"):
                add(value, label)
        code = element.get("onclick", "")
        for match in FILE_IN_CODE.finditer(code if isinstance(code, str) else ""):
            add(match.group(1), label)
    return list(links.items())
