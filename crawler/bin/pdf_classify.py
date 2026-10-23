"""Classify a downloaded Burmese PDF and extract its convertable text.

This is the scraper-side port of the linga site's two inspection layers,
kept to the SAME criteria so both ends of the pipeline agree on what a
document is:

* the frontend routing probe (frontend-bootstrap/js/detect.js + upload.js):
  whole-file image-byte scan, 32-page sampled text extrapolation, and the
  image-dominance / text-based thresholds;
* the parser's decoding ladder (Parsed/parser/pipeline.mjs streamPdfPages):
  identity probe -> merged embedded-font cmap -> /ToUnicode (+ sidecar
  cipher map), each gated by the Zawgyi/Unicode detector.

What is deliberately NOT here: OCR, legacy code-page voting, old-font
recovery. Old-font documents are preserved as PDFs only, and the verdict
is structural, not statistical: a document is old when its decoded text
contains no Myanmar script at all, or decodes too little of the book to
be a real reading (coverage floor), or fails the broken-text scan
(scan.py rules — marks Burmese cannot write) after conversion. The
Zawgyi/Unicode detector model has exactly one job here — splitting
genuine Myanmar text between the two encodings for conversion; it never
decides old vs convertable.

Categories:
  ``convertable``  Unicode or Zawgyi-One text: ``result.text`` holds the
                   converted + normalised document text;
  ``scanned``      image-only (no usable text layer): PDF preserved as-is;
  ``old``          text in a pre-standard font or otherwise undecodable:
                   PDF preserved as-is.

The classifier works on in-memory bytes: the downloader hands it the final
readable PDF (unlocked, repaired, linearized, no /ObjStm) before the file
becomes visible, so nothing is ever re-opened from disk.
"""

from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

_PRASER = Path(__file__).resolve().parents[2] / "praser"
if str(_PRASER) not in sys.path:
    sys.path.insert(0, str(_PRASER))

from cleanup import cleanup_text, order_text  # noqa: E402
from detector import ZawgyiDetector  # noqa: E402
from paths import find_model_path  # noqa: E402
from pdf_extract import (  # noqa: E402
    collect_pages,
    extract_page_lines,
    find_font_streams,
    find_root_pages,
    find_tounicode_maps,
    get_stream,
    load_sidecar_map,
    page_has_images,
    parse_pdf_objects,
    parse_ttf_cmap,
    pick_embedded_font,
)
from pdf_fonts import CmapIdentityMap, DocumentDecoding  # noqa: E402
from rabbit import Rabbit  # noqa: E402
from scan import scan_text  # noqa: E402

# ---------------------------------------------------------------------------
# Thresholds — same names and values as the frontend routing probe
# (frontend-bootstrap/js/detect.js) and the parser (pipeline.mjs).
# ---------------------------------------------------------------------------
PDF_TEXT_MIN_CHARS = 24       # doc is text-based at >= this many non-ws chars
PAGE_TEXT_MIN_CHARS = 12      # page counts as a text page at >= this
PDF_IMAGE_DOMINANCE = 20      # imageBytes > textBytes * this ...
PDF_TEXT_BYTES_PER_PAGE = 120  # ... AND textBytes/page < this => image-dominant
PDF_INSPECTION_MAX_PAGES = 32  # sampled pages, spread across the document
DETECT_PROBE_PAGES = 20        # decoding-ladder probe depth (pipeline.mjs)
MAX_TEXT_CHARS = 20_000_000    # extraction budget (core/limits.mjs default)

# Old-fonts verdict: structural scan of the CONVERTED text, not the model.
# The scan rules (scan.py) count things Burmese cannot write — orphan marks,
# impossible pairs, mark-only tokens, bare virama. A correct decode scores
# near zero; a wrong decode that still lands in the Myanmar block (a legacy
# ToUnicode, a misordered cmap) is saturated with them. Measured on this
# repo's corpus: correct converted book 0.00003, real dirty-but-genuine
# Unicode layer 0.00082, unconverted/wrong decode 0.127, in-range shuffled
# junk 0.163 issues per Myanmar character. 0.01 sits an order of magnitude
# above the dirtiest genuine text and an order below the cleanest broken one.
OLD_FONT_SCAN_RATE = 0.01
_SCAN_STRUCT_RULES = ("orphan", "impossible", "stray", "virama")

# Coverage gate: a decode that only accounts for a sliver of the book's
# codes produces structurally CLEAN but nearly EMPTY text — the scanner
# cannot see what is missing, and the model happily labels the sliver
# (measured on the shiyan book read through its own 134-entry ToUnicode:
# 1,042 of ~80,000 Myanmar chars decoded = ~10 per page, model claims
# ZAWGYI p=1.0, scan rate 0.0). A real Burmese book runs hundreds of
# Myanmar chars per page (shiyan via its font cmap: ~724/page; the dirty
# unicode sample: ~1,043/page). The floor reuses the frontend's
# PAGE_TEXT_MIN_CHARS: under 12 Myanmar chars per page on average, the
# decode did not really read the book -> old/, PDF preserved, no text.
MIN_MYANMAR_PER_PAGE = PAGE_TEXT_MIN_CHARS

_MYANMAR = ("ZAWGYI", "UNICODE")
_MY_CHAR = re.compile(r"[\u1000-\u109F]")
_MY_PAIR = re.compile(r"[\u1000-\u109F]{2}")

# Generated from Parser/parsed/codepage/names.mjs (LEGACY_FONT_NAMES);
# normalised with normalise_font_name below. 236 names.
LEGACY_FONT_NAMES = frozenset((
    "anonymouspro",
    "anonymousprobold",
    "anonymousprobolditalic",
    "anonymousproitalic",
    "anonymousproregular",
    "arkamm001tt",
    "arkaoriginal",
    "atypewriternormal",
    "cetitle",
    "nyicircle",
    "nyidesigner",
    "nyidesigner1",
    "nyidesigner2",
    "nyielephant",
    "nyihorizonal",
    "nyioval",
    "nyit",
    "nyithit1",
    "nyithit2",
    "nyithitf3",
    "nyithitfontfour",
    "nyithitfontone",
    "nyithitfonttwo",
    "nyitwice",
    "robin",
    "sakart",
    "sakmahura",
    "sakpadamya",
    "sakpeacock",
    "sakryoal",
    "sakseinarka",
    "sakthick",
    "saktwoline",
    "sakwide",
    "sakyatana",
    "softcrack",
    "u4ahlone",
    "u4bahan",
    "u4botataung",
    "u4dagonmyothit",
    "u4pazundaung",
    "u4thaketa",
    "w01arthouse",
    "w01arthouse2009",
    "w01arthousewin",
    "w02arthouse",
    "w02arthouse2009",
    "w02arthousewin",
    "w03arthouse",
    "w03arthouse2009",
    "w03arthousewin",
    "w04arthouse",
    "w04arthouse2009",
    "w04arthousewin",
    "w05arthouse",
    "w05arthouse2009",
    "w05arthousewin",
    "w06arthouse",
    "w06arthouse2009",
    "w06arthousewin",
    "w07arthouse",
    "w07arthouse2009",
    "w07arthousewin",
    "w08arthouse",
    "w08arthouse2009",
    "w08arthousewin",
    "w09arthouse",
    "w09arthouse2009",
    "w09arthousewin",
    "w10arthouse",
    "w10arthouse2009",
    "w10arthousewin",
    "w11arthouse",
    "w11arthouse2009",
    "w11arthousewin",
    "w12arthouse",
    "w12arthouse2009",
    "w12arthousewin",
    "w13arthouse",
    "w13arthouse2009",
    "w13arthousewin",
    "w14arthouse",
    "w14arthouse2009",
    "w14arthousewin",
    "w15arthouse",
    "w15arthouse2009",
    "w15arthousewin",
    "w16arthouse",
    "w16arthouse2009",
    "w16arthousewin",
    "w17arthouse",
    "w17arthouse2009",
    "w17arthousewin",
    "w18arthouse",
    "w18arthouse2009",
    "w18arthousewin",
    "w19arthouse",
    "w19arthouse2009",
    "w19arthousewin",
    "w20arthouse",
    "w20arthouse2009",
    "w20arthousewin",
    "w21arthouse",
    "w21arthouse2009",
    "w21arthousebold",
    "w22arthouse",
    "w22arthouse2009",
    "w23arthouse",
    "w23arthouse2009",
    "w24arthouse",
    "w24arthouse2009",
    "w25arthouse",
    "w25arthouse2009",
    "w26arthouse",
    "w26arthouse2009",
    "w27arthouse",
    "w27arthouse2009",
    "w28arthouse",
    "w28arthouse2009",
    "w29arthouse",
    "w29arthouse2009",
    "w30arthouse",
    "w30arthouse2009",
    "w31arthouse",
    "w31arthouse2009",
    "w31arthousebold",
    "w32arthouse",
    "w32arthouse2009",
    "w33arthouse",
    "w33arthouse2009",
    "w34arthouse",
    "w34arthouse2009",
    "w35arthouse",
    "w35arthouse2009",
    "w36arthouse",
    "w36arthouse2009",
    "w37arthouse",
    "w37arthouse2009",
    "w37arthousebold",
    "w38arthouse",
    "w38arthouse2009",
    "w39arthouse",
    "w39arthouse2009",
    "w40arthouse",
    "w40arthouse2009",
    "w41arthouse",
    "w41arthouse2009",
    "w42arthouse",
    "w42arthouse2009",
    "w43arthouse",
    "w43arthouse2009",
    "w44arthouse",
    "w44arthouse2009",
    "w45arthouse",
    "w45arthouse2009",
    "w46arthouse",
    "w46arthouse2009",
    "w47arthouse",
    "w47arthouse2009",
    "w48arthouse",
    "w48arthouse2009",
    "w49arthouse",
    "w49arthouse2009",
    "w50arthouse",
    "w50arthouse2009",
    "w51arthouse",
    "w51arthouse2009",
    "w52arthouse2009",
    "w53arthouse",
    "w53arthouse2009",
    "w54arthouse",
    "w54arthouse2009",
    "w55arthouse",
    "w55arthouse2009",
    "w56arthouse",
    "w56arthouse2009",
    "w57arthouse",
    "w57arthouse2009",
    "w58arthouse",
    "w58arthouse2009",
    "w59arthouse",
    "w59arthouse2009",
    "w60arthouse",
    "w60arthouse2009",
    "w61arthouse",
    "w61arthousebold",
    "w61arthousebolditalic",
    "w61arthouseextrabold",
    "w61arthouseitalic",
    "w62arthouse",
    "w62arthousebold",
    "w63arthouse",
    "w64arthouse",
    "w65arthouse",
    "w66arthouse",
    "w67arthouse",
    "w68arthouse",
    "w69arthouse",
    "w70arthouse",
    "whponnya",
    "whponnyabold",
    "winhaka",
    "wininnlay",
    "wininnlaya",
    "wininnwa",
    "wininnwa070",
    "wininnwa070a",
    "wininnwa080",
    "wininnwa080a",
    "wininnwa090",
    "wininnwa090a",
    "wininnwabold",
    "winkalaw",
    "winphaan",
    "winphaana",
    "winresearcher",
    "winresearchera",
    "wintaungyi",
    "wintaungyia",
    "winthanlyin",
    "winthanlyina",
    "winyadanapon",
    "winyadanapona",
    "winyangon",
    "winyangona",
    "wthawka",
    "wwinburmese",
    "wwinhanpagan",
    "wwinhanpagana",
    "wwinhanponnyacond",
    "wwinhlaing",
    "wwinhlaingmedium",
    "wwinponnyacond",
    "wwinponnyacondbold",
    "wwinponnyacondextrabold",
    "yadanabon",
))


def normalise_font_name(base_font) -> str:
    """Port of names.mjs normaliseFontName: #xx unescape, subset tag, lower."""
    s = re.sub(
        r"#([0-9A-Fa-f]{2})",
        lambda m: chr(int(m.group(1), 16)),
        str(base_font or ""),
    )
    s = re.sub(r"^[A-Z]{6}\+", "", s)
    return re.sub(r"[^a-z0-9]", "", s.lower())


def is_legacy_font_name(base_font) -> bool:
    """Port of names.mjs isLegacyFontName (exact + 6-char prefix match)."""
    n = normalise_font_name(base_font)
    if not n:
        return False
    if n in LEGACY_FONT_NAMES:
        return True
    for known in LEGACY_FONT_NAMES:
        if len(known) >= 6 and (n.startswith(known) or known.startswith(n)):
            return True
    return False


def is_zawgyi_one_font_name(base_font) -> bool:
    n = normalise_font_name(base_font)
    return n.startswith("zawgyione") or n in {"zawgyi1", "zawgyi"}


_BASE_FONT = re.compile(rb"/BaseFont\s*/((?:[^\s/<>\[\]()%]|#[0-9A-Fa-f]{2})+)")


def document_font_names(objects) -> set[str]:
    """Every /BaseFont name in the document, raw (un-normalised) form.

    ``parse_pdf_objects`` keeps objects as raw bytes, so the font dicts are
    scanned with a regex; /BaseFont appears only inside font dictionaries.
    """
    names: set[str] = set()
    for raw in objects.values():
        for m in _BASE_FONT.finditer(raw):
            names.add(m.group(1).decode("latin-1", errors="replace"))
    return names


# ---------------------------------------------------------------------------
# Whole-file image byte scan — port of detect.js pdfImageBytes
# ---------------------------------------------------------------------------
def pdf_image_bytes(data: bytes) -> int:
    """Total bytes inside /Subtype /Image stream bodies, whole file."""
    total = 0
    i = 0
    n = len(data)
    while i < n:
        mark = data.find(b"/Subtype", i)
        if mark < 0:
            break
        j = mark + 8
        while j < n and data[j] in b" \t\r\n\x0b\x0c":
            j += 1
        if not data.startswith(b"/Image", j):
            i = mark + 1
            continue
        st = data.find(b"stream", j)
        if st < 0:
            break
        body = st + 6
        if body < n and data[body] == 0x0D:
            body += 1
        if body < n and data[body] == 0x0A:
            body += 1
        end = data.find(b"endstream", body)
        if end < 0:
            break
        total += end - body
        i = end + 9
    return total


# ---------------------------------------------------------------------------
# Decoding ladder — port of pipeline.mjs streamPdfPages (legacy branch and
# OCR removed on purpose; Zawgyi-One font names jump straight to the cmap).
# ---------------------------------------------------------------------------
_DETECTOR: ZawgyiDetector | None = None


def get_detector() -> ZawgyiDetector:
    global _DETECTOR
    if _DETECTOR is None:
        _DETECTOR = ZawgyiDetector(find_model_path())
    return _DETECTOR


def _page_text(objects, pages, idx, decoding) -> str:
    pnum, pgen = pages[idx]
    lines = extract_page_lines(objects, pnum, pgen, decoding)[0]
    return "\n".join(line[3] for line in lines)


def _merged_font_cmap(objects) -> dict:
    """Merge every embedded font stream's cmap, best candidate first.

    Mirrors pipeline.mjs: ``pick_embedded_font`` chooses the font whose cmap
    carries Myanmar glyphs; its entries win gid conflicts (first font wins).
    """
    cmap_map: dict = {}
    font_refs = find_font_streams(objects)
    if not font_refs:
        return cmap_map
    ff_ref, ttf0, cmap0 = pick_embedded_font(objects, font_refs)
    ordered = []
    if ff_ref:
        ordered.append(ff_ref)
    for ref in font_refs:
        if not (ff_ref and ref[0] == ff_ref[0] and ref[1] == ff_ref[1]):
            ordered.append(ref)
    for ref in ordered:
        if ff_ref and ref[0] == ff_ref[0] and ref[1] == ff_ref[1]:
            cmap = cmap0
        else:
            ttf = get_stream(objects, ref[0], ref[1])
            cmap = parse_ttf_cmap(ttf) if ttf else {}
        if not cmap:
            continue
        for uni, gid in cmap.items():
            if not gid or gid in cmap_map:
                continue
            cmap_map[gid] = uni
    return cmap_map


def choose_decoding(objects, pages, zawgyi_one_named: bool):
    """Return (DocumentDecoding, kind_label) for this document.

    The gate for each ladder rung is deterministic: does the decoded probe
    text contain adjacent Myanmar-script characters at all? No statistical
    model is consulted here — the model's only job in this module is the
    Zawgyi/Unicode split, later.
    """
    npages = len(pages)
    probe_n = min(DETECT_PROBE_PAGES, npages)

    def decodes_myanmar(decoding) -> bool:
        for idx in range(probe_n):
            try:
                text = _page_text(objects, pages, idx, decoding)
            except Exception:  # noqa: BLE001 - one broken page must not stop the probe
                continue
            if _MY_PAIR.search(text):
                return True
        return False

    # Diagram rule: a document that names Zawgyi-One is decoded through the
    # embedded font cmap directly (glyph truth), no identity probe first.
    cmap_map = None
    if zawgyi_one_named:
        cmap_map = _merged_font_cmap(objects)
        if cmap_map:
            decoding = DocumentDecoding("cmap", CmapIdentityMap(cmap_map))
            if decodes_myanmar(decoding):
                return decoding, "cmap"

    # 1. Identity: the content-stream codes already are Unicode code points.
    direct = DocumentDecoding("identity")
    if decodes_myanmar(direct):
        return direct, "identity"

    # 2. Embedded font cmap (reversed uni->gid), merged across font streams.
    if cmap_map is None:
        cmap_map = _merged_font_cmap(objects)
    if cmap_map:
        decoding = DocumentDecoding("cmap", CmapIdentityMap(cmap_map))
        if decodes_myanmar(decoding):
            return decoding, "cmap"

    # 3. /ToUnicode maps merged with the sidecar cipher map.
    tu_map = find_tounicode_maps(objects)
    sidecar = load_sidecar_map()
    for code, uni in sidecar.items():
        tu_map.setdefault(code, uni)
    if tu_map:
        decoding = DocumentDecoding("tounicode", tu_map)
        if decodes_myanmar(decoding):
            return decoding, "tounicode"

    # Ungated fallbacks, same order as pipeline.mjs.
    if cmap_map:
        return DocumentDecoding("cmap", CmapIdentityMap(cmap_map)), "cmap"
    if tu_map:
        return DocumentDecoding("tounicode", tu_map), "tounicode"
    return DocumentDecoding("none"), "none"


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
@dataclass
class ClassifyResult:
    category: str                 # convertable | scanned | old
    encoding: str | None = None   # zawgyi-one | unicode (convertable only)
    pages: int = 0
    decode: str = "none"          # identity | cmap | tounicode | none
    text: str | None = None       # converted+normalised text (convertable)
    text_bytes: int = 0           # sampled text bytes extrapolated to the doc
    image_bytes: int = 0          # whole-file /Subtype /Image stream bytes
    sampled_pages: int = 0
    sampled_text_pages: int = 0
    sampled_image_pages: int = 0
    font_names: list[str] = field(default_factory=list)
    legacy_font: bool = False
    zawgyi_one_font: bool = False
    scan_counts: dict = field(default_factory=dict)  # broken-text rule totals
    scan_rate: float = 0.0        # structural issues per Myanmar character
    detail: str = ""
    ms: int = 0

    @property
    def folder(self) -> str:
        """Destination folder name under the output dir."""
        return {"convertable": "convertable", "scanned": "scanned",
                "old": "old"}[self.category]


def _convert_page(raw: str, detector) -> tuple[str, str]:
    """Port of pipeline.mjs processPageRecord (text only, cleanup applied)."""
    category, _prob = detector.detect(raw)
    if category == "ZAWGYI":
        return order_text(Rabbit.zg2uni(raw)), category
    if category == "UNICODE":
        base = order_text(raw)
        clean = cleanup_text(base)
        return (clean if clean else base), category
    return order_text(raw), "UNKNOWN"


def classify_pdf(data: bytes, detector: ZawgyiDetector | None = None) -> ClassifyResult:
    """Classify final PDF bytes; extract converted text when convertable."""
    started = time.monotonic()
    detector = detector or get_detector()
    result = ClassifyResult(category="scanned")
    result.image_bytes = pdf_image_bytes(data)

    try:
        objects = parse_pdf_objects(data)
        root = find_root_pages(objects)
        if isinstance(root, tuple):
            pages = collect_pages(objects, *root)
        elif root is not None:
            pages = collect_pages(objects, root)
        else:
            pages = []
    except Exception as error:  # noqa: BLE001 - a bad file must not stop the run
        result.detail = f"praser could not parse the PDF: {error}"
        result.ms = int((time.monotonic() - started) * 1000)
        return result

    npages = len(pages)
    result.pages = npages
    if not npages:
        result.detail = "no page tree found"
        result.ms = int((time.monotonic() - started) * 1000)
        return result

    raw_names = document_font_names(objects)
    result.font_names = sorted(normalise_font_name(n) for n in raw_names)
    result.legacy_font = any(is_legacy_font_name(n) for n in raw_names)
    result.zawgyi_one_font = any(is_zawgyi_one_font_name(n) for n in raw_names)

    decoding, kind = choose_decoding(objects, pages, result.zawgyi_one_font)
    result.decode = kind

    # --- frontend routing probe: sampled pages spread across the document ---
    sample_count = min(npages, PDF_INSPECTION_MAX_PAGES)
    result.sampled_pages = sample_count
    if sample_count == 1:
        indexes = [0]
    else:
        indexes = [
            round(i * (npages - 1) / (sample_count - 1))
            for i in range(sample_count)
        ]
    parts = []
    for idx in indexes:
        try:
            page_text = _page_text(objects, pages, idx, decoding)
        except Exception:  # noqa: BLE001 - one broken page must not stop the doc
            page_text = ""
        parts.append(page_text)
        if len(re.sub(r"\s", "", page_text)) >= PAGE_TEXT_MIN_CHARS:
            result.sampled_text_pages += 1
        else:
            pnum, pgen = pages[idx]
            try:
                if page_has_images(objects, pnum, pgen):
                    result.sampled_image_pages += 1
            except Exception:  # noqa: BLE001
                pass
    sampled_text = re.sub(r"\n{3,}", "\n\n", "\n".join(parts)).strip()
    scale = npages / sample_count
    result.text_bytes = round(len(sampled_text.encode("utf-8")) * scale)

    per_page = result.text_bytes / npages if npages else result.text_bytes
    image_dominant = (
        result.image_bytes > result.text_bytes * PDF_IMAGE_DOMINANCE
        and per_page < PDF_TEXT_BYTES_PER_PAGE
    )
    nonws = len(re.sub(r"\s", "", sampled_text))
    text_based = not image_dominant and nonws >= PDF_TEXT_MIN_CHARS

    if not text_based:
        result.category = "scanned"
        result.detail = (
            f"image-dominant ({result.image_bytes} image bytes vs "
            f"{result.text_bytes} text bytes)"
            if image_dominant
            else f"no usable text layer ({nonws} non-ws chars sampled)"
        )
        result.ms = int((time.monotonic() - started) * 1000)
        return result

    # Deterministic old-fonts gate #1: the decoded sample carries no Myanmar
    # script at all (legacy faces that type Burmese on ASCII codes — the
    # text layer decodes to Latin glyph-soup). No model involved.
    if not _MY_PAIR.search(sampled_text):
        result.category = "old"
        result.detail = (
            "legacy Myanmar font names present; text decodes to no Myanmar"
            if result.legacy_font
            else "text layer decodes to no Myanmar script"
        )
        result.ms = int((time.monotonic() - started) * 1000)
        return result

    # The model's ONLY decision: is this Myanmar text Zawgyi or Unicode?
    # (It cannot answer "is this a correct decode" — that is the scanner's
    # job below.)
    category, _prob = detector.detect(sampled_text)
    encoding = "zawgyi-one" if category == "ZAWGYI" else "unicode"

    # --- full extraction + conversion, page by page ---
    converted: list[str] = []
    total_chars = 0
    truncated = False
    for idx in range(npages):
        try:
            raw = _page_text(objects, pages, idx, decoding)
        except Exception:  # noqa: BLE001
            raw = ""
        page_out, _page_cat = _convert_page(raw, detector) if raw else ("", "")
        converted.append(page_out)
        total_chars += len(page_out)
        if total_chars > MAX_TEXT_CHARS:
            truncated = True
            break
    text = re.sub(r"\n{3,}", "\n\n", "\n\n".join(converted)).strip()
    try:
        report = scan_text(text)
        result.scan_counts = {
            rule: info.get("total", 0) for rule, info in report.items()
        }
    except Exception:  # noqa: BLE001 - the verdict needs the scan; fail old
        result.scan_counts = {}
        result.category = "old"
        result.detail = "broken-text scan failed; kept as old (no conversion)"
        result.ms = int((time.monotonic() - started) * 1000)
        return result

    # Old-fonts gate #2 — coverage: did the decode actually read the book?
    # Structural cleanliness means nothing when almost nothing decoded.
    my_chars = len(_MY_CHAR.findall(text))
    if my_chars / npages < MIN_MYANMAR_PER_PAGE:
        result.category = "old"
        result.detail = (
            f"decode covers too little of the book "
            f"({my_chars} Myanmar chars over {npages} pages = "
            f"{my_chars / npages:.1f}/page, floor {MIN_MYANMAR_PER_PAGE}); "
            "kept as PDF only, no conversion"
        )
        result.ms = int((time.monotonic() - started) * 1000)
        return result

    # Old-fonts gate #3 — THE decider: structural validity of the CONVERTED
    # text. The scan runs after Rabbit/ordering/cleanup, so repairable
    # damage has already been repaired; what remains is what the decode
    # really produced. A wrong decode that lands in the Myanmar block
    # (legacy ToUnicode, misordered cmap) scores ~0.13+ issues per Myanmar
    # character; genuine text scores ~0.001 or less.
    issues = sum(result.scan_counts.get(rule, 0) for rule in _SCAN_STRUCT_RULES)
    result.scan_rate = issues / my_chars if my_chars else 1.0
    if result.scan_rate > OLD_FONT_SCAN_RATE:
        result.category = "old"
        result.detail = (
            f"decoded text fails the structural scan "
            f"({issues} issues / {my_chars} Myanmar chars = "
            f"{result.scan_rate:.4f} > {OLD_FONT_SCAN_RATE}); "
            "kept as PDF only, no conversion"
        )
        result.ms = int((time.monotonic() - started) * 1000)
        return result

    result.category = "convertable"
    result.encoding = encoding
    result.text = text
    result.detail = (
        f"{result.encoding} via {kind} decode; "
        f"scan rate {result.scan_rate:.5f}"
    )
    if truncated:
        result.detail += f"; text truncated at {MAX_TEXT_CHARS} chars"
    result.ms = int((time.monotonic() - started) * 1000)
    return result


def extract_convertable_text(
    data: bytes, detector: ZawgyiDetector | None = None
) -> tuple[str, str, int]:
    """Extract and convert a PDF's whole text layer with NO classification gates.

    Returns ``(text, decode_kind, pages)``; ``text`` is empty when the file has no
    readable text layer at all. A PDF that cannot be parsed raises, so the caller
    records a real reason instead of silently storing nothing.

    Why this exists next to ``classify_pdf``: that function's job is to DECIDE what
    an unknown PDF is, and it refuses the text of anything it judges ``scanned`` or
    ``old`` - image dominance, too few Myanmar characters per page, or a structural
    scan rate above ``OLD_FONT_SCAN_RATE``. Those gates are the old-font verdict
    (WinNwa, Ayar, the legacy layout methods), and they are the right call for a PDF
    nobody has looked at. They are the wrong call for a PDF already sitting in
    ``convertable/``: the owner put it there precisely because its text survives
    intact, and re-judging it with the parser he does not trust can only lose a book
    he has already verified. So this keeps the stable half of the pipeline - parse
    the objects, pick a decode from the cmap / identity / ToUnicode ladder, extract
    every page, convert Zawgyi to Unicode - and drops every gate and every verdict.
    No category is returned because none is decided.
    """
    detector = detector or get_detector()
    objects = parse_pdf_objects(data)
    root = find_root_pages(objects)
    if isinstance(root, tuple):
        pages = collect_pages(objects, *root)
    elif root is not None:
        pages = collect_pages(objects, root)
    else:
        pages = []
    if not pages:
        return "", "none", 0

    # The ladder's Zawgyi-One shortcut needs the font NAMES, so read them - but only
    # to pick a decode. Nothing here turns a name into a verdict.
    raw_names = document_font_names(objects)
    zawgyi_one_named = any(is_zawgyi_one_font_name(n) for n in raw_names)
    decoding, kind = choose_decoding(objects, pages, zawgyi_one_named)

    converted: list[str] = []
    total_chars = 0
    truncated = False
    for idx in range(len(pages)):
        try:
            raw = _page_text(objects, pages, idx, decoding)
        except Exception:  # noqa: BLE001 - one broken page must not stop the book
            raw = ""
        page_out, _page_cat = _convert_page(raw, detector) if raw else ("", "")
        converted.append(page_out)
        total_chars += len(page_out)
        if total_chars > MAX_TEXT_CHARS:
            truncated = True
            break
    text = re.sub(r"\n{3,}", "\n\n", "\n\n".join(converted)).strip()
    if truncated:
        print(f"[parse-warn] text truncated at {MAX_TEXT_CHARS} chars", file=sys.stderr)
    return text, kind, len(pages)


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: pdf_classify.py FILE.pdf [FILE.pdf ...]")
        return 2
    for name in argv:
        data = Path(name).read_bytes()
        r = classify_pdf(data)
        print(
            f"{Path(name).name}: {r.category}"
            + (f" ({r.encoding})" if r.encoding else "")
            + f" pages={r.pages} decode={r.decode}"
            + f" textBytes={r.text_bytes} imageBytes={r.image_bytes}"
            + (f" textChars={len(r.text)}" if r.text else "")
            + f" scan={r.scan_counts}"
            + f" [{r.ms} ms] {r.detail}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
