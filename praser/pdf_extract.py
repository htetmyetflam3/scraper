"""Low-level PDF parsing and text-layout extraction.

Parses raw PDF bytes into objects/streams, reads embedded font streams
(``/FontFile``, ``/FontFile2``, ``/FontFile3``) and their ``cmap`` tables,
picks the font whose cmap actually contains Myanmar glyphs, reads ``/ToUnicode``
CMap streams (needed for OpenType/CFF and CID fonts that have no ``cmap``),
walks the page tree, and extracts text while preserving layout (line breaks from
text positioning).

Text decoding itself is NOT done here: the content stream is walked operator by
operator, the ``Tf`` operator selects one of the page's ``/Font`` resources, and
every shown string is handed to that resource's decoder in :mod:`pdf_fonts`.
That is what keeps a Latin ``/WinAnsiEncoding`` resource and a Myanmar
``/Identity-H`` resource apart even when they share one embedded font program.
"""

import re
import os
import zlib

from mm_score import MARKS
from pdf_fonts import (
    CMapEncoding,
    CmapIdentityMap,
    DocumentDecoding,
    FontDecoder,
    FontResourceLoader,
    HexString,
    IDENTITY,
    Keyword,
    LegacyAyarMap,
    AYAR_LEGACY_CODE_PAGE,
    LEGACY_CODE_PAGES,
    WIN_ANSI_ENCODING,
    detect_legacy_layout_fonts,
    Lexer,
    Name,
    Ref,
    as_document_decoding,
    map_code,
    page_font_decoders,
    parse_object,
    usable_tounicode,
)

# Re-exported for callers that imported these from this module before the
# font-specific decoders were split out (pipeline.py, tests, the JS port docs).
_map_code = map_code

__all__ = [
    "CmapIdentityMap", "DocumentDecoding", "IDENTITY", "LegacyAyarMap",
    "ARLARWADE_AYAR_MAP", "AYAR_LEGACY_CODE_PAGE", "LEGACY_CODE_PAGES",
    "detect_legacy_layout_fonts", "MAX_STREAM_MB", "as_document_decoding",
    "collect_pages", "extract_metadata", "extract_page_lines",
    "extract_page_text_layout", "find_font_streams", "find_font_files2",
    "find_root_pages", "find_tounicode_maps", "get_page_mediabox",
    "get_stream", "iter_content_operations", "legacy_ayar_to_unicode",
    "fix_digit_consonant_confusion",
    "load_sidecar_map", "map_code", "page_font_decoders", "page_has_images",
    "parse_pdf_objects", "parse_tounicode_stream", "parse_ttf_cmap",
    "pick_embedded_font", "zlib_decompress_capped",
]

# ── decompression-bomb guard ─────────────────────────────────
# The praser is the ONLY component that opens containers, so it is the
# one that carries the cap: a tiny compressed stream must never be able
# to expand into gigabytes inside this process. Env-tunable.
MAX_STREAM_MB = int(os.environ.get("PRASER_MAX_STREAM_MB", "256"))


def zlib_decompress_capped(b, limit_mb=None):
    """zlib.decompress with a hard output cap — bomb-safe."""
    limit = (MAX_STREAM_MB if limit_mb is None else limit_mb) * 1024 * 1024
    out = bytearray()
    dobj = zlib.decompressobj()
    data = b
    while data:
        out += dobj.decompress(data, 1 << 20)
        if len(out) > limit:
            raise ValueError(
                f"decompressed stream exceeds {MAX_STREAM_MB} MB cap "
                "(decompression bomb?)"
            )
        data = dobj.unconsumed_tail
        if dobj.eof:
            break
    out += dobj.flush()
    if len(out) > limit:
        raise ValueError(
            f"decompressed stream exceeds {MAX_STREAM_MB} MB cap "
            "(decompression bomb?)"
        )
    return bytes(out)
import struct


def parse_pdf_objects(raw):
    obj_pat = re.compile(rb'(\d+)\s+(\d+)\s+obj')
    objects = {}
    for m in obj_pat.finditer(raw):
        n, g = int(m.group(1)), int(m.group(2))
        start = m.end()
        end = raw.find(b'endobj', start)
        if end == -1:
            continue
        objects[(n, g)] = raw[start:end].strip()
    return objects


def get_stream(objects, num, gen=0):
    d = objects.get((num, gen), b'')
    i = d.find(b'stream')
    if i == -1:
        return None
    j = i + 6
    if j < len(d) and d[j:j+1] == b'\r':
        j += 1
    if j < len(d) and d[j:j+1] == b'\n':
        j += 1
    k = d.rfind(b'endstream')
    if k == -1:
        return None
    b = d[j:k]
    if b'/FlateDecode' in d[:i]:
        try:
            return zlib_decompress_capped(b)
        except ValueError:
            raise  # bomb guard: refuse the document loudly, never silently
        except Exception:
            return None
    return b


def extract_metadata(objects):
    metadata = {}
    for (n, g), d in objects.items():
        d_str = d.decode('latin-1', errors='replace')
        if re.search(r'/Type\s*/Catalog', d_str):
            info_m = re.search(r'/Info\s+(\d+)\s+\d+\s+R', d_str)
            if info_m:
                info_n = int(info_m.group(1))
                info_d = objects.get((info_n, 0), b'').decode('latin-1', errors='replace')
                for kv in re.finditer(r'/([^\s/\[\]<>()]+)\s*\(([^)]*)\)', info_d):
                    metadata[kv.group(1)] = kv.group(2)
            break
    return metadata


# Matches an embedded font stream reference of any kind:
#   /FontFile    (Type 1)
#   /FontFile2   (TrueType)
#   /FontFile3   (OpenType/CFF, often subtype /OpenType or /CIDFontType0C)
_FONT_STREAM_RE = re.compile(r'/FontFile(?:2|3)?\s+(\d+)\s+(\d+)\s+R')


def find_font_streams(objects):
    """Return a list of all embedded font-stream object refs in the PDF.

    A PDF can embed many fonts (e.g. Zawgyi-One, Calibri, MS Gothic, Times New
    Roman), and they may be stored as ``/FontFile`` (Type 1), ``/FontFile2``
    (TrueType) or ``/FontFile3`` (OpenType/CFF). We collect every candidate so
    the caller can pick the font whose cmap actually contains Myanmar glyphs
    instead of blindly using the first.
    """
    refs = []
    for (n, g), d in objects.items():
        d_str = d.decode('latin-1', errors='replace')
        for m in _FONT_STREAM_RE.finditer(d_str):
            ref = (int(m.group(1)), int(m.group(2)))
            if ref not in refs:
                refs.append(ref)
    return refs


# Backwards-compatible alias (older callers looked for TrueType only).
find_font_files2 = find_font_streams


def _utf16be_to_str(hexstr):
    """Decode a UTF-16BE hex string (e.g. from a ToUnicode CMap) to text."""
    if len(hexstr) % 2:
        hexstr = '0' + hexstr
    raw = bytes.fromhex(hexstr)
    if len(raw) % 2:
        raw = b'\x00' + raw
    return raw.decode('utf-16-be', errors='replace')


def parse_tounicode_stream(data):
    """Parse a PDF ``/ToUnicode`` CMap stream into ``{int code: str unicode}``.

    Handles the ``bfchar`` (single mappings) and ``bfrange`` (scalar and array
    ranges) operators used by the common ``CMapType 2`` ToUnicode maps. This is
    the authoritative code→Unicode mapping for CID-keyed fonts (CFF/OpenType),
    which often have no usable ``cmap`` table.
    """
    try:
        text = data.decode('latin-1')
    except Exception:
        return {}

    mapping = {}

    # bfchar: <src> <dst>  (dst is UTF-16BE)
    for m in re.finditer(r'beginbfchar(.*?)endbfchar', text, re.DOTALL):
        for p in re.finditer(r'<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>', m.group(1)):
            mapping[int(p.group(1), 16)] = _utf16be_to_str(p.group(2))

    # bfrange, scalar form: <lo> <hi> <start>
    for m in re.finditer(r'beginbfrange(.*?)endbfrange', text, re.DOTALL):
        body = m.group(1)
        for p in re.finditer(r'<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>', body):
            lo, hi = int(p.group(1), 16), int(p.group(2), 16)
            if hi < lo or hi - lo > 0xFFFF:
                continue
            dst_hex = p.group(3)
            if len(dst_hex) <= 4:
                start = int(dst_hex, 16)
                for code in range(lo, hi + 1):
                    cp = start + (code - lo)
                    if 0 <= cp <= 0x10FFFF and not (0xD800 <= cp <= 0xDFFF):
                        mapping[code] = chr(cp)
            else:
                if len(dst_hex) % 4:
                    dst_hex = dst_hex.zfill((len(dst_hex) + 3) // 4 * 4)
                prefix_hex = dst_hex[:-4]
                last_unit = int(dst_hex[-4:], 16)
                for code in range(lo, hi + 1):
                    unit = (last_unit + (code - lo)) & 0xFFFF
                    mapping[code] = _utf16be_to_str(f"{prefix_hex}{unit:04X}")

    # bfrange, array form: <lo> <hi> [<d1> <d2> ...]
    for m in re.finditer(r'beginbfrange(.*?)endbfrange', text, re.DOTALL):
        body = m.group(1)
        for p in re.finditer(r'<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\[(.*?)\]', body, re.DOTALL):
            lo, hi = int(p.group(1), 16), int(p.group(2), 16)
            if hi < lo or hi - lo > 0xFFFF:
                continue
            items = re.findall(r'<([0-9A-Fa-f]+)>', p.group(3))
            for i, code in enumerate(range(lo, hi + 1)):
                if i < len(items):
                    mapping[code] = _utf16be_to_str(items[i])

    return mapping


def find_tounicode_maps(objects):
    """Collect every ``/ToUnicode`` CMap in the PDF into ``{code: unicode}``.

    Scans all objects for ``/ToUnicode N G R`` references, decompresses the
    referenced stream, and merges the resulting code→Unicode maps (first
    mapping wins for a given code).

    Copy-protected PDFs ship *blanked* ToUnicode maps (nearly every code maps
    to a space) as decoys. Those are detected (>90% space values) and only
    their non-space entries are kept, so they cannot poison the merge.
    """
    code_to_uni = {}
    seen = set()
    for (n, g), d in objects.items():
        d_str = d.decode('latin-1', errors='replace')
        if '/ToUnicode' not in d_str:
            continue
        for m in re.finditer(r'/ToUnicode\s+(\d+)\s+\d+\s+R', d_str):
            tn = int(m.group(1))
            if tn in seen:
                continue
            seen.add(tn)
            s = get_stream(objects, tn)
            if not s:
                continue
            mp = parse_tounicode_stream(s)
            if not mp:
                continue
            spaces = sum(1 for v in mp.values() if v == ' ')
            if spaces / len(mp) > 0.9:
                # blanked decoy: only its few non-space entries are real
                mp = {k: v for k, v in mp.items() if v != ' '}
            for code, uni in mp.items():
                code_to_uni.setdefault(code, uni)
    return code_to_uni


def load_sidecar_map(path=None):
    """Optional cross-volume cipher map — the SHARED ``<root>/map/cipher-map.json``.

    Some volumes ship only blanked ToUnicode maps; sibling volumes of the same
    series share the font cipher, so their valid maps (merged once into the
    sidecar JSON as ``{hex_code: unicode}``) can decode them. The document's
    own valid entries always take precedence (caller merges with setdefault).
    One copy for both ports: Parser/parsed/read/pdf-extract.mjs reads the
    same file through its MAP_DIR.
    """
    import json
    from pathlib import Path
    p = Path(path) if path else Path(__file__).resolve().parent.parent / "map" / "cipher-map.json"
    if not p.is_file():
        return {}
    try:
        return {int(k, 16): v for k, v in json.loads(p.read_text(encoding="utf-8")).items()}
    except Exception:
        return {}


def get_page_mediabox(objects, page_num, page_gen):
    """Return (width, height) of a page's MediaBox in PDF points.

    Walks up the /Parent chain (MediaBox is usually inherited from /Pages).
    Falls back to US Letter (612 x 792).
    """
    d = objects.get((page_num, page_gen), b'')
    for _ in range(8):
        m = re.search(rb'/MediaBox\s*\[\s*([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s*\]', d)
        if m:
            x0, y0, x1, y1 = (float(v) for v in m.groups())
            return abs(x1 - x0), abs(y1 - y0)
        pm = re.search(rb'/Parent\s+(\d+)\s+(\d+)\s+R', d)
        if not pm:
            break
        d = objects.get((int(pm.group(1)), int(pm.group(2))), b'')
    return 612.0, 792.0


def page_has_images(objects, page_num, page_gen):
    """True when the page's resources declare image XObjects (image pages).

    Resolves the full indirection chain: page -> /Resources (inline or ref)
    -> /XObject dict (inline or ref) -> its entries, which may be inline
    dicts or refs to the image-stream objects carrying /Subtype /Image.
    """
    def _get(ref_match):
        raw = objects.get((int(ref_match.group(1)), 0), b'')
        return raw.decode('latin-1', errors='replace')

    d = objects.get((page_num, page_gen), b'').decode('latin-1', errors='replace')
    res = d
    m = re.search(r'/Resources\s+(\d+)\s+\d+\s+R', d)
    if m:
        res = _get(m)
    if re.search(r'/Subtype\s*/Image', res):
        return True                       # inline image dict in resources
    mx = re.search(r'/XObject\s+(\d+)\s+\d+\s+R', res)
    xo = _get(mx) if mx else res          # XObject dict: indirect or inline
    if re.search(r'/Subtype\s*/Image', xo):
        return True
    for rm in re.finditer(r'(\d+)\s+\d+\s+R', xo):
        if re.search(r'/Subtype\s*/Image', _get(rm)):
            return True                   # image stream behind an entry ref
    return False


_MYA_RANGE = range(0x1000, 0x109F + 1)


def _has_myanmar_glyphs(cmap):
    """True if the cmap maps any Myanmar Unicode codepoint to a glyph."""
    return any(c in _MYA_RANGE for c in cmap)


def pick_embedded_font(objects, refs, log=lambda m: None):
    """Choose the embedded font whose cmap contains Myanmar glyphs.

    Falls back to the first candidate when none contains Myanmar glyphs, and
    returns (ref, ttf_bytes, cmap). ref/ttf/cmap are None/{} when nothing usable
    was found.
    """
    fallback = None
    for ref in refs:
        ttf = get_stream(objects, *ref)
        if not ttf:
            continue
        cmap = parse_ttf_cmap(ttf)
        if _has_myanmar_glyphs(cmap):
            log(f"[+] Chosen embedded font obj {ref[0]} (contains Myanmar glyphs)")
            return ref, ttf, cmap
        if fallback is None:
            fallback = (ref, ttf, cmap)

    if fallback is not None:
        ref, ttf, cmap = fallback
        log(f"[i] No Myanmar-glyph font found; using first font stream obj {ref[0]}")
        return ref, ttf, cmap

    return None, None, {}


def parse_ttf_cmap(data):
    if len(data) < 12:
        return {}
    num_tables = struct.unpack('>H', data[4:6])[0]
    tbls = {}
    off = 12
    for _ in range(num_tables):
        if off + 16 > len(data):
            break
        tag = data[off:off+4].decode('ascii', errors='replace')
        tbl_offset = struct.unpack('>I', data[off+8:off+12])[0]
        tbls[tag] = tbl_offset
        off += 16
    if 'cmap' not in tbls:
        return {}
    cmap_off = tbls['cmap']
    if cmap_off + 4 > len(data):
        return {}
    cmap = data[cmap_off:]
    num_sub = struct.unpack('>H', cmap[2:4])[0]
    mappings = {}
    off = 4
    for _ in range(num_sub):
        if off + 8 > len(cmap):
            break
        plat, enc = struct.unpack('>HH', cmap[off:off+4])
        sub_off = struct.unpack('>I', cmap[off+4:off+8])[0]
        off += 8
        if sub_off + 2 > len(cmap):
            continue
        sub = cmap[sub_off:]
        if len(sub) < 2:
            continue
        fmt = struct.unpack('>H', sub[0:2])[0]
        if fmt == 4:
            if len(sub) < 14:
                continue
            seg_count = struct.unpack('>H', sub[6:8])[0] // 2
            if len(sub) < 14 + seg_count * 8:
                continue
            ends = [struct.unpack('>H', sub[14+i*2:16+i*2])[0] for i in range(seg_count)]
            so = 14 + seg_count * 2 + 2
            starts = [struct.unpack('>H', sub[so+i*2:so+2+i*2])[0] for i in range(seg_count)]
            do = so + seg_count * 2
            deltas = [struct.unpack('>h', sub[do+i*2:do+2+i*2])[0] for i in range(seg_count)]
            ro = do + seg_count * 2
            ranges = [struct.unpack('>H', sub[ro+i*2:ro+2+i*2])[0] for i in range(seg_count)]
            for j in range(seg_count):
                if starts[j] == 0xFFFF:
                    continue
                for c in range(starts[j], ends[j] + 1):
                    if ranges[j] == 0:
                        gid = (c + deltas[j]) & 0xFFFF
                    else:
                        idx = ranges[j] // 2 + (c - starts[j]) + j - seg_count
                        if ro + idx * 2 + 2 > len(sub):
                            continue
                        gid = struct.unpack('>H', sub[ro + idx*2:ro + idx*2 + 2])[0]
                        if gid:
                            gid = (gid + deltas[j]) & 0xFFFF
                    if gid:
                        mappings[c] = gid
        elif fmt == 0:
            # Format 0 -- the byte encoding table: 256 one-byte glyph ids, no
            # code points above 0xFF. This is the ONLY subtable a Type 42
            # legacy Myanmar subset carries, so before this branch every
            # lookup on such a font came back empty and the embedded glyphs
            # were invisible. (Ported from the current JS core/ttf-cmap.mjs.)
            count = min(256, len(sub) - 6)
            if count < 8:
                continue
            for c in range(count):
                gid = sub[6 + c]
                if gid:
                    mappings[c] = gid
        elif fmt == 6:
            if len(sub) < 10:
                continue
            first_code = struct.unpack('>H', sub[6:8])[0]
            entry_count = struct.unpack('>H', sub[8:10])[0]
            for j in range(entry_count):
                if 10 + j * 2 + 2 > len(sub):
                    break
                gid = struct.unpack('>H', sub[10+j*2:12+j*2])[0]
                if gid:
                    mappings[first_code + j] = gid
        elif fmt == 12:
            if len(sub) < 16:
                continue
            num_groups = struct.unpack('>I', sub[12:16])[0]
            go = 16
            for _ in range(num_groups):
                if go + 12 > len(sub):
                    break
                sc, ec, sg = struct.unpack('>III', sub[go:go+12])
                go += 12
                if sc > 0x10FFFF or ec < sc:
                    continue
                ec = min(ec, 0x10FFFF)
                for c in range(sc, ec + 1):
                    mappings[c] = sg + (c - sc)
    return mappings


def collect_pages(objects, page_num, gen=0):
    d = objects.get((page_num, gen), b'')
    d_str = d.decode('latin-1', errors='replace')
    if re.search(r'/Type\s*/Page(?!s)', d_str):
        return [(page_num, gen)]
    if re.search(r'/Type\s*/Pages', d_str):
        pages = []
        km = re.search(rb'/Kids\s*\[(.*?)\]', d, re.DOTALL)
        if km:
            kids_text = km.group(1).decode('latin-1', errors='replace')
            for ref in re.findall(r'(\d+)\s+\d+\s+R', kids_text):
                pages.extend(collect_pages(objects, int(ref)))
        return pages
    return []


def find_root_pages(objects):
    for (n, g), d in objects.items():
        d_str = d.decode('latin-1', errors='replace')
        if re.search(r'/Type\s*/Catalog', d_str):
            pm = re.search(r'/Pages\s+(\d+)\s+\d+\s+R', d_str)
            if pm:
                return int(pm.group(1))
    return None


# ``IDENTITY``, ``CmapIdentityMap`` and ``LegacyAyarMap`` now live in
# pdf_fonts (they are decoding semantics, shared by every font decoder) and
# are re-exported above.

# Arlarwade is an old Ayar-compatible Myanmar font.  The embedded subsets in
# mm-grammar.pdf are not Unicode encoded: their PDF character codes 33..121
# select the same legacy glyph table used by Ayar.  The candidate Ayar font in
# fonts/Unicode is used as the visual reference; these are the resulting
# character-code -> Ayar/Unicode values.  Some entries are multi-character
# legacy glyphs (notably 81 = stacked ဒ္ဒ) and 92 is a zero-width/placeholder
# glyph that must be discarded rather than emitted as a raw backslash.
#
# Keep this as a code map, not a glyph-id map.  PDF TrueType subsets renumber
# glyph IDs independently, while the character codes remain stable across all
# Arlarwade subsets in the document.  (The ``LegacyAyarMap`` marker class it is
# wrapped in lives in pdf_fonts with the other decoding semantics.)
ARLARWADE_AYAR_MAP = {
    33: "ြ", 34: "ပ", 35: "ည", 36: "်", 37: "ေ", 38: "ထ",
    39: "ာ", 40: "င", 41: "စ", 42: "ု", 43: "သ", 44: "မ",
    45: "္မ", 46: "တ", 47: "န", 48: "န", 49: "ိ", 50: "ံ",
    51: "ရ", 52: "း", 53: "၀", 54: "ြ", 55: "က", 56: "ီ",
    57: "ဌ", 58: "အ", 59: "ဖ", 60: "ွ", 61: "ဲ", 62: "့",
    63: "ဥ", 64: "ဒ", 65: "ယ", 66: "ှ", 67: "ခ", 68: "၂",
    69: "၀", 70: "၁", 71: "၃", 72: "—", 73: "၊", 74: " ",
    75: "လ", 76: "ူ", 77: "။", 78: "က", 79: "ျ", 80: "ါ",
    81: "္ဒ", 82: "ါ", 83: "၅", 84: "ဇ", 85: "ံ", 86: "့",
    87: "ြ", 88: "ု", 89: "၂", 90: "၍", 91: "ဝ", 92: "",
    93: "ဉ", 94: "ဏ", 95: "ဆ", 96: "၉", 97: "၈", 98: "၄",
    99: "ဘ", 100: "၆", 101: "ဤ", 102: "ြ", 103: "ခ", 104: "ဂ",
    105: "", 106: "ဗ", 107: "ရ", 108: "(", 109: ")", 110: "ဃ",
    111: "၌", 112: "ဈ", 113: "ဍ", 114: "ဎ", 115: "၊", 116: "ဝ",
    117: "စ", 118: "ဗ", 119: "ဝ", 120: "ၺ", 121: "ပ",
}


def fix_digit_consonant_confusion(text):
    """Restore the consonant ``ဝ`` where a legacy face wrote the digit ``၀``.

    U+101D (the consonant wa) and U+1040 (digit zero) are the same shape, and
    legacy code pages routinely resolve the shared glyph to the digit. The
    disambiguation is structural: a digit cannot carry a dependent mark, so a
    ``၀`` wearing one is really the consonant.

    Two structural cases, and nothing else:

    1. a digit wearing a dependent MARK;
    2. a digit with a Myanmar letter or mark on BOTH sides -- it is inside a
       word, and a number is not. This is the common one: the neighbour in
       front is usually a mark, not a base (``ပါ၀င်`` ×327, ``တာ၀န်``, ``တာ၀တိံ``,
       ``သာ၀က``), so requiring a base in front finds nothing at all.

    A digit is never a letter or a mark, so a real number is untouched from
    either side: ``၁၀``, ``၅၀၊`` and a ``၀`` after a space stay digits.

    The same glyph runs the other way too: ``\u101b`` (ra) and ``\u1047`` (digit seven)
    are one shape, so a ``\u101b`` INSIDE a run of digits is the digit -- ``\u1041\u101b`` is
    17 and ``\u1043\u1048\u101b`` is 387. Never when a letter or mark follows, or ``\u1045\u101b\u1000\u103a``
    (five days) would lose its ``\u101b\u1000\u103a``.

    On a 445-page legacy grammar this takes orphan marks from 4.47% to 2.20%,
    and the later cases convert a further ~1,700 occurrences.
    """
    def letter_or_mark(ch):
        # ch may be "" at a string edge, and "" is `in` every string
        return bool(ch) and ("\u1000" <= ch <= "\u1021"
                             or ch in ("\u1025", "\u1027", "\u1039")
                             or ch in MARKS)

    def digit(ch):
        return bool(ch) and "\u1040" <= ch <= "\u1049"

    out = list(text)
    n = len(out)
    for i in range(n):
        after = out[i + 1] if i + 1 < n else ""
        before = out[i - 1] if i else ""
        if out[i] == "\u1040":
            # ၀ -> ဝ : the digit is wearing a mark, or sits inside a word
            if after in MARKS or (letter_or_mark(before) and letter_or_mark(after)):
                out[i] = "\u101d"
            # ...or it OPENS a word: a number does not run into a consonant
            elif not digit(before) and not letter_or_mark(before) \
                    and "\u1000" <= after <= "\u1021":
                out[i] = "\u101d"
    text = "".join(out)

    # ရ and ၇ are the same glyph, and the test is simply this: the shape is
    # ရ unless a digit sits next to it. ရက် always carries its က်, so a
    # following Myanmar letter or mark means ရ -- while a following digit, or
    # another ရ, keeps it numeric.
    #
    #   ၅ရက်   -> ၅ရက်    (five days; က follows, so it is the word)
    #   ရ၅ကတ္တား -> ၇၅ ကတ္တား  (index: the page number runs into the entry)
    #   ၃ရရ     -> ၃၇၇      (page 377)
    #   ၅၀ရာ    -> ၅၀ရာ     (ာ follows, so it is ရာ)
    #
    # Repeated to a fixed point so a run of ရ resolves from either end:
    # ရရ၅ needs the rightmost to become ၇ before the leftmost can see a digit.
    for _ in range(3):
        chars = list(text)
        changed = False
        for i, ch in enumerate(text):
            if ch != "\u101b":
                continue
            before = text[i - 1] if i else ""
            after = text[i + 1] if i + 1 < len(text) else ""
            if (digit(before) or digit(after)) and (
                    not letter_or_mark(after) or digit(after) or after == "\u101b"):
                chars[i] = "\u1047"
                changed = True
        text = "".join(chars)
        if not changed:
            break
    return text


_PREPOSED_RE = re.compile(
    r"([ြေ]+)"                       # the preposed vowel(s)
    r"([က-အ၀၈၇])"                 # its base
    r"(င်္)?"                       # kinzi, written after the base
    r"((?:္[က-အ])*)"                # any stacked consonants
)


def _collapse_overprint(text):
    """Collapse a piece that is one short unit drawn twice (or three times).

    The faux-bold here is not only a second COPY of a run at a small offset
    (that is handled by the piece dedup); the producer also repeats the
    glyphs INSIDE one text-showing operation, so a single piece arrives as
    ``\u1019\u1019`` or ``\u103e\u103e`` or ``\u102c\u1038\u102c\u1038``. No piece-level comparison can see
    that -- there is only one piece.

    Deliberately narrow, because Burmese does repeat syllables for real
    (``\u1019\u103c\u1014\u103a\u1019\u103c\u1014\u103a``, ``\u1021\u101e\u102e\u1038\u101e\u102e\u1038``): the WHOLE piece must be the unit repeated,
    the unit at most 3 characters, and the piece at most 9 -- a real word
    does not arrive as its own isolated fragment.
    """
    n = len(text)
    if n < 2 or n > 9:
        return text
    for size in (1, 2, 3):
        if n % size or n // size < 2:
            continue
        unit = text[:size]
        if unit * (n // size) == text:
            return unit
    return text


def legacy_ayar_to_unicode(text):
    """Put Ayar's visual/legacy order into normal Myanmar code-point order.

    This is intentionally small and conservative.  It mirrors the established
    Ayar -> Myanmar3 conversion rules: move preposed ြ/ေ after the base,
    move င်္ clusters into their Unicode order, and remove the legacy ZWSP.
    """
    text = text.replace("\u200b", "")
    # ONE left-to-right pass for the preposed vowels. Two passes cannot work:
    # the first would place သေကင်္တ correctly as သင်္ကေတ, and the second
    # would then see that same ေ sitting in front of တ and move it AGAIN,
    # giving သင်္ကတေ. Matching once and continuing past the replacement
    # is what keeps it put.
    #
    # The vowel clears the whole cluster, never landing inside it:
    #   base            ေက        -> ကေ
    #   stacked pair    ေဏ္ဏ     -> ဏ္ဏေ      (not ဏေ္ဏ)
    #   kinzi           ေကင်္    -> င်္ကေ     (kinzi leads the cluster)
    text = _PREPOSED_RE.sub(
        lambda m: (m.group(3) or "") + m.group(2) + (m.group(4) or "") + m.group(1),
        text)

    # A kinzi that is NOT followed by a consonant cannot belong to one on its
    # right, so it belongs to the cluster on its left and moves in front of
    # it: သချင်္ာ -> သင်္ချာ. When a consonant DOES follow, the kinzi is
    # already where it belongs (ဝါစင်္ဂ) and is left alone -- 460 of those
    # here against 24 of the broken shape.
    #
    # This repairs the AUTHOR'S TYPING, not a decoding error. The raw stream
    # really holds \u101e \u1001 \u103b \u1004\u103a\u1039 \u102c (medial BEFORE kinzi) for all 24
    # occurrences -- the order the legacy input method does not accept. The
    # two encodings differ in how loudly they say so: Zawgyi renders that
    # order visibly wrong, while Unicode reorders on the fly and renders it
    # correctly, so in Unicode the same mistake is INVISIBLE and only the
    # code points show it. That is why the matching scanner check compares
    # code points and never shapes.
    #
    # It has to run AFTER the preposed vowel has been placed, and the group
    # it jumps must include that vowel. သေဘင်္ာ is သင်္ဘော: moving the
    # kinzi first leaves it sitting behind ေ, and the preposed rule then
    # splits it apart and destroys it (18 သင်္ဘော, 7 တနင်္ဂနွေ, 6 ခြင်္သေ့
    # were lost that way).
    text = re.sub(r"([\u1000-\u1021][\u102b-\u103e]*)"
                  r"(\u1004\u103a\u1039)(?![\u1000-\u1021])",
                  r"\2\1", text)
    text = re.sub(r"(ေ)([ျွဲှ္ြ]+)", r"\2\1", text)
    text = re.sub(r"([က-အ])([က-အ])(င်္)", r"\1\3\2", text)
    # Ayar writes ဦး as ဦး in a number of older subsets.
    text = text.replace("ဦး", "ဦး")

    # Reorder-by-priority is NOT done here. It belongs to exactly one place,
    # cleanup.reorder_marks, which reads the single shared priority table;
    # a second implementation here drifted from it once already. What stays
    # is what only this function can do: the legacy font's visual order.
    #
    # The single-word substitution that used to live here (a ``ဲ``/``ွ`` mix-up
    # in one Arlarwade subset) is gone: per-resource decoding removed the
    # cause, and patching one word of one document was never a fix.
    # A digit cannot carry a mark; restore the consonant so the marks that
    # follow have a base to attach to.
    return fix_digit_consonant_confusion(text)


# ═══════════════════════════════════════════════════════════════════════════
#  Content-stream walking
# ═══════════════════════════════════════════════════════════════════════════

# An inline image's binary data is not PDF syntax, so the tokenizer must jump
# over it instead of trying to read it.  ``EI`` ends the image and is always
# preceded by whitespace.
_INLINE_IMAGE_END_RE = re.compile(rb"[\s\x00]EI(?=[\s\x00\[\]<>/%(]|$)")


def _skip_inline_image(data, pos):
    m = _INLINE_IMAGE_END_RE.search(data, pos)
    return m.end() if m else len(data)


def iter_content_operations(data):
    """Yield ``(operator, operands)`` of a content stream, in document order.

    A real (if small) PDF tokenizer, not a per-block regex: text-showing
    operators have to be seen in sequence with the ``Tf`` operators around
    them, because the active font persists across ``BT...ET`` blocks and one
    page routinely mixes a Latin resource with a Myanmar one.
    """
    lex = Lexer(data)
    operands = []
    containers = []
    while True:
        tok = lex.next_token()
        if tok is None:
            break
        kind, value = tok
        if kind == "aopen":
            containers.append(("a", len(operands)))
            continue
        if kind == "dopen":
            containers.append(("d", len(operands)))
            continue
        if kind in ("aclose", "dclose"):
            if not containers:
                continue
            ctype, mark = containers.pop()
            items = operands[mark:]
            del operands[mark:]
            if ctype == "a":
                operands.append(items)
            else:
                d = {}
                for i in range(0, len(items) - 1, 2):
                    if isinstance(items[i], Name):
                        d[str(items[i])] = items[i + 1]
                operands.append(d)
            continue
        if kind == "kw":
            if containers:                       # e.g. "n g R" inside an array
                operands.append(value)
                continue
            op = str(value)
            if op == "ID":                       # inline image data follows
                lex.pos = _skip_inline_image(data, lex.pos)
                operands = []
                continue
            yield op, operands
            operands = []
            continue
        operands.append(value)
        if not containers and len(operands) > 64:
            # Malformed stream guard: an operator never takes more than a
            # handful of operands. Only trimmed at the top level — a TJ array
            # legitimately holds hundreds of strings and kerning numbers.
            del operands[:-16]


class _UnsetFontDecoder(FontDecoder):
    """Decoder used for text shown before any ``Tf`` selects a resource.

    There is no font to ask, so the document-level map is applied straight to
    the codes — exactly what this parser did for every string before decoding
    became font-specific.  Hex strings keep their two-byte reading, literal
    strings their one-byte reading.

    One important asymmetry, ported from the current JS (read/pdf-fonts.mjs
    UnsetFontDecoder): when the document decoding is the embedded-font cmap,
    its keys are GLYPH IDS. A hex string under Identity-H really does carry
    glyph ids, so that branch is right. A one-byte literal string does not:
    it carries character codes for a simple font, and pushing those through
    a gid table silently renumbers every character by the font's glyph
    order. In the 100-page Zawgyi-One sample, Zawgyi-One lays glyphs out at
    gid = ascii - 29, so byte 0x28 "(" resolved to gid 40, whose cmap entry
    is "E"; ")" became "F". Byte codes therefore go through WinAnsiEncoding
    — the standard reading for a simple font — and only reach the gid table
    when WinAnsi has nothing to say.
    """

    __slots__ = ()

    def decode(self, data):
        if not data:
            return ""
        if isinstance(data, HexString):
            out = []
            for i in range(0, len(data) - 1, 2):
                code = (data[i] << 8) | data[i + 1]
                text = self._doc_lookup(code)
                out.append(text if text is not None else f"[{code:04X}]")
            return "".join(out)
        byte_codes = (getattr(self.doc, "key_kind", "code") == "gid"
                      and 0 not in data)
        win_ansi = WIN_ANSI_ENCODING if byte_codes else None
        out = []
        for code in data:
            if win_ansi is not None:
                simple = win_ansi.get(code)
                if simple is not None:
                    out.append(simple)
                    continue
            text = self._doc_lookup(code)
            out.append(text if text is not None else chr(code))
        return "".join(out)


def _document_loader(doc, objects):
    """One :class:`FontResourceLoader` per document, cached on the strategy."""
    loader = doc.loader
    if loader is None or loader.objects is not objects:
        loader = FontResourceLoader(objects, get_stream, parse_ttf_cmap,
                                    parse_tounicode_stream)
        doc.loader = loader
        # Decoders are cached by object number, which only identifies a font
        # within one document.
        doc.reset_font_cache()
    return loader


def _shown_strings(op, operands):
    """Return the strings a text-showing operator draws (in order)."""
    if not operands:
        return []
    if op == "Tj" or op == "'":
        last = operands[-1]
        return [last] if isinstance(last, bytes) else []
    if op == '"':
        last = operands[-1]
        return [last] if isinstance(last, bytes) else []
    if op == "TJ":
        last = operands[-1]
        if isinstance(last, list):
            return [item for item in last if isinstance(item, bytes)]
    return []


def extract_page_lines(objects, page_num, page_gen, decoding, detect_columns=False):
    """Extract text lines WITH layout: ``(lines, page_w, page_h)``.

    ``lines`` is a top-to-bottom list of ``(x, y, size, text)`` where x/y are
    PDF points (y measured from the page BOTTOM, as in the PDF user space) and
    size is the Tf font size — everything the exact-layout DOCX writer needs
    to reproduce the page. Page size comes from the (inherited) MediaBox.

    ``detect_columns`` enables the multi-column splitter (the gutter guard
    below). OFF by default: paragraph rewrap (short first piece + long
    continuation piece on one baseline) mimics a gutter, and telling them
    apart needs tracked x-advances (line widths) the regex parser does not
    collect yet. Verified against a synthetic two-column page; tune against
    real samples before turning it on.

    ``decoding`` is the document-level fallback strategy
    (:class:`pdf_fonts.DocumentDecoding`); a bare map or the ``IDENTITY``
    sentinel is still accepted and wrapped. It is NOT applied to the page as a
    whole: each ``/Font`` resource of the page gets its own decoder and the
    ``Tf`` operator decides which one every shown string goes through.
    """
    doc = as_document_decoding(decoding)
    loader = _document_loader(doc, objects)
    decoders = page_font_decoders(loader, page_num, page_gen, doc)
    unset_font = _UnsetFontDecoder("", doc=doc)

    pd = objects.get((page_num, page_gen), b'')
    pd_str = pd.decode('latin-1', errors='replace')
    streams = []
    cm = re.search(r'/Contents\s+(\d+)\s+\d+\s+R', pd_str)
    if cm:
        streams = [int(cm.group(1))]
    else:
        ca = re.search(r'/Contents\s*\[(.*?)\]', pd_str, re.DOTALL)
        if ca:
            streams = [int(x) for x in re.findall(r'(\d+)\s+\d+\s+R', ca.group(1))]

    pieces = []  # list of (y, x, size, text)

    # The active text font survives BT/ET boundaries and, because a page's
    # content streams are one stream logically, the stream boundary too. The
    # title of the sample book is drawn in a BT...ET block that contains no Tf
    # at all and inherits the Latin resource selected several blocks earlier —
    # which is precisely what a per-block regex cannot see.
    active = None

    for sn in streams:
        s = get_stream(objects, sn)
        if not s:
            continue

        in_block = False
        block_texts = []
        block_xy = None
        block_size = None
        text_xy = (0.0, 0.0)
        leading = 0.0

        for op, operands in iter_content_operations(s):
            if op == "Tf":
                if len(operands) >= 2 and isinstance(operands[-2], Name):
                    active = decoders.get(str(operands[-2]))
                if in_block and operands and isinstance(operands[-1], (int, float)):
                    # Font size of the block: the last Tf inside it, as before.
                    block_size = float(operands[-1])
                continue

            if op == "BT":
                if not in_block:
                    in_block = True
                    block_texts = []
                    block_xy = None
                    block_size = None
                    # BT resets the text matrix to the identity, i.e. the
                    # origin. Everything below tracks the line matrix from
                    # there.
                    text_xy = (0.0, 0.0)
                    leading = 0.0
                continue

            if not in_block:
                continue

            if op == "Tm":
                if len(operands) >= 6 \
                        and all(isinstance(v, (int, float)) for v in operands[-6:]):
                    text_xy = (float(operands[-2]), float(operands[-1]))
                    if block_xy is None:
                        block_xy = text_xy
                continue

            # Td/TD/T* position text just as legitimately as Tm, and some
            # producers never emit Tm at all. Tracking only Tm left block_xy
            # as None for those, and the block was then dropped at ET -- the
            # whole document came out empty even though every glyph decoded
            # correctly.
            if op in ("Td", "TD"):
                if len(operands) >= 2 \
                        and all(isinstance(v, (int, float)) for v in operands[-2:]):
                    dx, dy = float(operands[-2]), float(operands[-1])
                    # Both translate the LINE matrix, so they are relative to
                    # the start of the current line, not the current point.
                    text_xy = (text_xy[0] + dx, text_xy[1] + dy)
                    if op == "TD":
                        leading = -dy
                    if block_xy is None:
                        block_xy = text_xy
                continue

            if op == "TL":
                if operands and isinstance(operands[-1], (int, float)):
                    leading = float(operands[-1])
                continue

            if op == "T*":
                text_xy = (text_xy[0], text_xy[1] - leading)
                if block_xy is None:
                    block_xy = text_xy
                continue

            if op == "ET":
                in_block = False
                if block_texts and block_xy is not None:
                    # NOT rewritten here. A legacy fix needs the characters
                    # on either side, and this PDF splits words across
                    # pieces constantly -- 4,666 pieces begin with a bare
                    # dependent mark. Per-piece, every rule that looks at a
                    # neighbour fails silently at the seam: the preposed
                    # vowel finds no base, the kinzi finds no consonant, and
                    # \u1040 keeps its digit form because its mark is in the NEXT
                    # piece. The fix runs once the line is assembled.
                    text = ''.join(block_texts)
                    if doc.legacy_text_fix:
                        text = _collapse_overprint(text)
                    pieces.append((block_xy[1], block_xy[0],
                                   12.0 if block_size is None else block_size,
                                   text))
                block_texts = []
                continue

            if op in ("Tj", "TJ", "'", '"'):
                decoder = active if active is not None else unset_font
                for shown in _shown_strings(op, operands):
                    block_texts.append(decoder.decode(shown))

    # PDF producers often draw the same glyph run several times with tiny
    # sub-point offsets to create a faux-bold/outline effect.  Those are visual
    # shadows, not five copies of the sentence.  Collapse exact decoded runs
    # that overlap, while retaining genuinely repeated text at different
    # locations.
    #
    # CONTAINMENT, not just equality. The overprint is not always a copy of
    # the whole run: this producer also re-draws a FRAGMENT of it, and a
    # fragment is not equal to anything, so it survived and was spliced back
    # into the middle of the line by the x-sort. One line carried
    #     x=306.72 'စပါး၏ ဆုိလုိရင်းအနက်မ'   x=403.26 'ှ'
    #     x=403.26 'ာ ‘ပူအုိက်စ'          x=306.92 'စပါး'  <- shadow
    # and assembled as အနက်မစပါးှာ instead of အနက်မှာ စပါး. The ±1pt box
    # still guards it: genuinely repeated text elsewhere on the line is
    # farther away than that.
    unique_pieces = []
    for piece in pieces:
        y, x, size, text = piece
        drop = False
        for k, (old_y, old_x, old_size, old_text) in enumerate(unique_pieces):
            if abs(x - old_x) > 1.0 or abs(y - old_y) > 1.0:
                continue
            if text == old_text or text in old_text:
                drop = True            # same ink, or a fragment of it
                break
            if old_text in text:
                # the shadow arrived first: keep the longer, fuller run
                unique_pieces[k] = piece
                drop = True
                break
        if not drop:
            unique_pieces.append(piece)
    pieces = unique_pieces

    page_w, page_h = get_page_mediabox(objects, page_num, page_gen)
    if not pieces:
        return [], page_w, page_h

    # ── multi-column guard ────────────────────────────────────────────────
    # A gutter only EXISTS where two columns share the same baseline band —
    # hanging indents and margin labels sit on their own baselines and are
    # already in reading order under the plain y-sort, so they must never
    # trigger a split. Gutter candidates therefore come ONLY from mixed
    # bands (left + right pieces on one baseline) with a wide internal gap;
    # a consistent candidate across enough mixed bands + balanced text mass
    # switches the page to column-major order. Every other page keeps the
    # single-column path below byte for byte.
    #
    # GATED OFF by default (detect_columns=False): paragraph rewrap produces
    # exactly this mixed-band pattern too, and without tracked x-advances
    # the two cannot be told apart reliably (test.pdf regressed). See the
    # docstring.
    bounds = []
    if detect_columns:
        pieces.sort(key=lambda p: (-p[0], p[1]))
        mixed_bands = []
        cur, cur_y = [], pieces[0][0]
        for p in pieces:
            if abs(p[0] - cur_y) > 3:
                if len(cur) >= 2:
                    mixed_bands.append(cur)
                cur, cur_y = [], p[0]
            cur.append(p)
        if len(cur) >= 2:
            mixed_bands.append(cur)

        gutters = []
        for items in mixed_bands:
            items = sorted(items, key=lambda q: q[1])
            for a, b in zip(items, items[1:]):
                if b[1] - a[1] >= 14:
                    gutters.append((a[1] + b[1]) / 2)
        if gutters and len(mixed_bands) >= 5:
            gutters.sort()
            clusters = [[gutters[0]]]
            for g in gutters[1:]:
                if g - clusters[-1][-1] <= 15:
                    clusters[-1].append(g)
                else:
                    clusters.append([g])
            best = max(clusters, key=len)
            if len(best) >= max(5, int(0.5 * len(mixed_bands))):
                c = sum(best) / len(best)
                left = sum(len(t) for _, x, _, t in pieces if x < c)
                right = sum(len(t) for _, x, _, t in pieces if x >= c)
                tot = left + right
                if tot and 0.25 <= left / tot <= 0.75:
                    bounds = [c]

        if bounds:
            lines = []
            for c in range(len(bounds) + 1):
                col_pieces = [p for p in pieces if sum(1 for b in bounds if p[1] >= b) == c]
                col_pieces.sort(key=lambda p: (-p[0], p[1]))
                cur_line, cur_y = [], None
                for y, x, size, text in col_pieces:
                    if cur_y is None or abs(y - cur_y) > 3:
                        if cur_line:
                            cur_line.sort(key=lambda p: p[0])   # x only; stable = stream order
                            _j = ''.join(t for _, _, t in cur_line)
                            if doc.legacy_text_fix:
                                _j = legacy_ayar_to_unicode(_j)
                            lines.append((cur_line[0][0], cur_y,
                                          max(s for _, s, _ in cur_line), _j))
                        cur_line, cur_y = [], y
                    cur_line.append((x, size, text))
                if cur_line:
                    cur_line.sort(key=lambda p: p[0])   # x only; stable = stream order
                    lines.append((cur_line[0][0], cur_y,
                                  max(s for _, s, _ in cur_line),
                                  ''.join(t for _, _, t in cur_line)))
            return lines, page_w, page_h

    pieces.sort(key=lambda p: (-p[0], p[1]))

    lines = []          # (x, y, size, text), top-to-bottom
    current_y = pieces[0][0]
    current_line = []   # (x, size, text)

    def flush():
        # Sort by x ONLY, and rely on the sort being stable: two pieces drawn
        # at the same x keep content-stream order, which is the order the
        # producer wrote them in.
        #
        # This is what was dropping/moving medial ha (U+103E). The legacy face
        # draws a tall consonant's ha as its own BT..ET block placed at the x
        # of the NEXT block, so a line ends up as
        #     ... 'ရိ'(x=298) | 'ှ'(x=358) | 'သည်။'(x=358)
        # and a tie-break on the text put 'ှ' (U+103E) after 'သ' (U+101E),
        # stranding the ha at the end of the line: 'ရိသည်။ှ'. Stream order
        # puts it back on its base: 'ရှိသည်။'.
        ordered = sorted(current_line, key=lambda p: p[0])
        joined = ''.join(t for _, _, t in ordered)
        if doc.legacy_text_fix:
            joined = legacy_ayar_to_unicode(joined)
        lines.append((ordered[0][0], current_y,
                      max(s for _, s, _ in ordered), joined))

    for y, x, size, text in pieces:
        if abs(y - current_y) > 3:
            flush()
            current_line = []
            current_y = y
        current_line.append((x, size, text))

    if current_line:
        flush()

    return lines, page_w, page_h


def extract_page_text_layout(objects, page_num, page_gen, decoding):
    """Extract text preserving PDF layout (line breaks from text positioning).

    Thin wrapper over :func:`extract_page_lines` returning just the joined
    text (identical output to the original implementation).
    """
    lines, _, _ = extract_page_lines(objects, page_num, page_gen, decoding)
    return '\n'.join(text for _, _, _, text in lines)
