"""Font-specific PDF text decoding — ONE decoder per ``/Font`` resource.

Why this module exists
----------------------
A PDF page selects fonts by *resource name* (``/F1``, ``/F2``, ...) with the
``Tf`` operator, and every shown string must be decoded with the rules of the
font that is active at that moment. Two resources on the same page routinely

  * point at the SAME embedded font program, and
  * declare COMPLETELY DIFFERENT encodings.

The classic case (and the bug this module fixes) is a Burmese book whose pages
carry::

    /F1  Type0 /Identity-H  -> Myanmar, content codes are CIDs/glyph ids
    /F2  TrueType /WinAnsiEncoding -> Latin, content codes are character codes

both descending from one embedded ``Zawgyi-One`` subset. Decoding the whole
page with a single document-wide "glyph id -> Unicode" map (the reverse of the
embedded ``cmap``) silently reinterprets Latin character codes as glyph ids:
``G`` (0x47) renders as ``d``, ``S`` (0x53) as ``p`` — ``God of Slaughter``
comes out as ``dod of plaughter``. No cleanup rule can repair that, because the
damage happens during extraction.

So decoding is resolved per font resource:

  ``page -> /Resources -> /Font -> {name: FontDecoder}``

and :mod:`pdf_extract` tracks the ``Tf`` operator while walking the content
stream so every ``Tj`` / ``TJ`` / ``'`` / ``"`` string is handed to the decoder
of the font that is actually selected — including when the active font was set
in an earlier ``BT...ET`` block and simply persists.

Decoding order
--------------
Simple fonts (``/Type1``, ``/TrueType``, ``/Type3``, ``/MMType1``):

  1. the font's OWN ``/ToUnicode`` map (when it is usable and the document
     strategy does not distrust ToUnicode maps),
  2. the font's declared ``/Encoding``: ``/WinAnsiEncoding``,
     ``/MacRomanEncoding``, ``/StandardEncoding`` or an encoding dictionary
     with ``/BaseEncoding`` + ``/Differences`` (glyph names),
  3. the document-level fallback map (legacy code map / reversed embedded
     ``cmap`` / merged ToUnicode+sidecar) — reached only by fonts that declare
     NO encoding, i.e. the symbolic legacy faces this parser was written for,
  4. the raw byte as a character (unchanged, as before this module existed).

Composite fonts (``/Type0``):

  1. the font's OWN ``/ToUnicode`` map (same trust rule),
  2. CID -> GID through ``/CIDToGIDMap``, then the reversed ``cmap`` of the
     font's OWN embedded program, then the document-level map,
  3. ``[XXXX]`` placeholder.

Code widths come from the ``/Encoding`` CMap (``/Identity-H`` and embedded CMap
streams: ``codespacerange`` + ``cidrange`` / ``cidchar``), never assumed.

The module deliberately knows nothing about :mod:`pdf_extract`; the few stream
primitives it needs are injected through :class:`FontResourceLoader`, which
keeps the import graph acyclic (``pdf_fonts`` <- ``pdf_extract``).
"""

import re
import struct
import unicodedata


# ═══════════════════════════════════════════════════════════════════════════
#  1. Minimal PDF object syntax (shared by object bodies and content streams)
# ═══════════════════════════════════════════════════════════════════════════

class Name(str):
    """A PDF ``/Name`` (kept distinct from a string object)."""
    __slots__ = ()

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"/{str(self)}"


class Keyword(str):
    """A bare PDF keyword / content-stream operator (``Tj``, ``R``, ``true``)."""
    __slots__ = ()


class HexString(bytes):
    """A string written as ``<48656C6C6F>``.

    Identical to a literal string as far as a font decoder is concerned — the
    font decides how wide a character code is. The distinction is kept only so
    that text shown before any ``Tf`` (no active font at all) can still be
    read the way this parser always read it: hex in two-byte CIDs.
    """
    __slots__ = ()


class Ref(tuple):
    """An indirect reference ``n g R``."""
    __slots__ = ()

    def __new__(cls, num, gen=0):
        return super().__new__(cls, (int(num), int(gen)))

    @property
    def num(self):
        return self[0]

    @property
    def gen(self):
        return self[1]

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"{self[0]} {self[1]} R"


_TOKEN_RE = re.compile(
    rb"[\s\x00]*(?:"
    rb"(?P<comment>%[^\r\n]*)"
    rb"|(?P<name>/[^\s\x00()<>\[\]{}/%]*)"
    rb"|(?P<num>[+-]*(?:\d+\.\d*|\.\d+|\d+))"
    rb"|(?P<dopen><<)"
    rb"|(?P<dclose>>>)"
    rb"|(?P<hex><[^>]*>)"
    rb"|(?P<aopen>\[)"
    rb"|(?P<aclose>\])"
    rb"|(?P<lit>\()"
    rb"|(?P<popen>\{)"
    rb"|(?P<pclose>\})"
    rb"|(?P<kw>[A-Za-z'\"*][A-Za-z0-9'\"*]*)"
    rb"|(?P<other>[^\s\x00])"
    rb")"
)

_LIT_SPECIAL_RE = re.compile(rb"[()\\\r]")

_ESCAPES = {
    0x6E: b"\n",   # \n
    0x72: b"\r",   # \r
    0x74: b"\t",   # \t
    0x62: b"\b",   # \b
    0x66: b"\f",   # \f
    0x28: b"(",
    0x29: b")",
    0x5C: b"\\",
}


def scan_literal_string(data, i):
    """Scan a literal ``(...)`` string body starting at ``i`` (after the ``(``).

    Returns ``(raw_bytes, end_index)``. Implements the full PDF escape set —
    ``\\(``, ``\\)``, ``\\\\``, ``\\n \\r \\t \\b \\f``, three-digit octal
    ``\\ddd``, backslash-newline line continuations — and nested balanced
    parentheses. A bare end-of-line inside the string becomes a line feed, as
    the specification requires.
    """
    depth = 1
    out = bytearray()
    n = len(data)
    while i < n:
        m = _LIT_SPECIAL_RE.search(data, i)
        if m is None:
            out += data[i:]
            return bytes(out), n
        out += data[i:m.start()]
        c = m.group()
        i = m.end()
        if c == b"(":
            depth += 1
            out += b"("
        elif c == b")":
            depth -= 1
            if depth == 0:
                return bytes(out), i
            out += b")"
        elif c == b"\r":
            # EOL inside a string is a single line feed (\r, \r\n -> \n)
            if i < n and data[i:i + 1] == b"\n":
                i += 1
            out += b"\n"
        else:  # backslash
            if i >= n:
                break
            b = data[i]
            i += 1
            if b in _ESCAPES:
                out += _ESCAPES[b]
            elif 0x30 <= b <= 0x37:                      # octal \ddd
                val = b - 0x30
                for _ in range(2):
                    if i < n and 0x30 <= data[i] <= 0x37:
                        val = val * 8 + (data[i] - 0x30)
                        i += 1
                    else:
                        break
                out += bytes([val & 0xFF])
            elif b == 0x0A:                              # line continuation
                pass
            elif b == 0x0D:
                if i < n and data[i:i + 1] == b"\n":
                    i += 1
            else:
                out += bytes([b])                        # \x -> x
    return bytes(out), n


def _hex_to_bytes(text):
    """``<48656C 6C6F>`` -> ``b'Hello'`` (odd trailing digit padded with 0)."""
    digits = re.sub(rb"[^0-9A-Fa-f]", b"", text)
    if len(digits) % 2:
        digits += b"0"
    try:
        return bytes.fromhex(digits.decode("ascii"))
    except ValueError:                                   # pragma: no cover
        return b""


class Lexer:
    """Token scanner over PDF syntax. Also used for content streams."""

    __slots__ = ("data", "pos")

    def __init__(self, data, pos=0):
        self.data = data
        self.pos = pos

    def next_token(self):
        """Return ``(kind, value)`` or ``None`` at end of data.

        ``kind`` is one of ``obj`` (a complete simple object), ``kw``,
        ``aopen``, ``aclose``, ``dopen``, ``dclose``.
        """
        data = self.data
        while True:
            m = _TOKEN_RE.match(data, self.pos)
            if not m or m.end() == m.start():
                return None
            self.pos = m.end()
            kind = m.lastgroup
            if kind == "comment":
                continue
            if kind == "name":
                return "obj", Name(_decode_name(m.group("name")[1:]))
            if kind == "num":
                raw = m.group("num")
                try:
                    if b"." in raw:
                        return "obj", float(raw.lstrip(b"+"))
                    return "obj", int(raw.lstrip(b"+"))
                except ValueError:
                    return "obj", 0
            if kind == "hex":
                return "obj", HexString(_hex_to_bytes(m.group("hex")[1:-1]))
            if kind == "lit":
                s, end = scan_literal_string(data, self.pos)
                self.pos = end
                return "obj", s
            if kind == "kw":
                kw = m.group("kw").decode("latin-1")
                if kw == "true":
                    return "obj", True
                if kw == "false":
                    return "obj", False
                if kw == "null":
                    return "obj", None
                return "kw", Keyword(kw)
            if kind in ("aopen", "aclose", "dopen", "dclose"):
                return kind, None
            if kind in ("popen", "pclose"):
                continue                                  # PostScript proc
            return "kw", Keyword(m.group(kind).decode("latin-1"))


_NAME_ESC_RE = re.compile(rb"#([0-9A-Fa-f]{2})")


def _decode_name(raw):
    """``Times#20New`` -> ``Times New``."""
    raw = _NAME_ESC_RE.sub(lambda m: bytes([int(m.group(1), 16)]), raw)
    return raw.decode("latin-1")


def _collapse_refs(stack):
    """Fold trailing ``n g R`` triples on an operand stack into a :class:`Ref`."""
    if len(stack) >= 3 and isinstance(stack[-1], Keyword) and stack[-1] == "R":
        gen, num = stack[-2], stack[-3]
        if isinstance(num, int) and isinstance(gen, int):
            del stack[-3:]
            stack.append(Ref(num, gen))
            return True
    return False


def parse_object(data, pos=0):
    """Parse the FIRST complete object in ``data``; return it (or ``None``)."""
    lex = Lexer(data, pos)
    stack = []
    containers = []
    while True:
        tok = lex.next_token()
        if tok is None:
            break
        kind, value = tok
        if kind == "aopen":
            containers.append(("a", len(stack)))
            continue
        if kind == "dopen":
            containers.append(("d", len(stack)))
            continue
        if kind in ("aclose", "dclose"):
            if not containers:
                continue
            ctype, mark = containers.pop()
            items = stack[mark:]
            del stack[mark:]
            if ctype == "a":
                stack.append(items)
            else:
                d = {}
                for i in range(0, len(items) - 1, 2):
                    key = items[i]
                    if isinstance(key, Name):
                        d[str(key)] = items[i + 1]
                stack.append(d)
        elif kind == "kw":
            stack.append(value)
            _collapse_refs(stack)
            if not containers and value in ("stream", "endobj"):
                break
        else:
            stack.append(value)
        if not containers and stack and not isinstance(stack[-1], Keyword):
            # a complete top-level object (refs need their trailing R first)
            if len(stack) == 1 and not isinstance(stack[0], int):
                return stack[0]
            if isinstance(stack[-1], Ref):
                return stack[-1]
    for item in stack:
        if not isinstance(item, Keyword):
            return item
    return None


# ═══════════════════════════════════════════════════════════════════════════
#  2. Encodings for simple fonts
# ═══════════════════════════════════════════════════════════════════════════

def _codec_encoding(codec, undefined=None, overrides=None):
    """Build ``{code: char}`` for codes 0x20..0xFF from a Python codec."""
    table = {}
    for code in range(0x20, 0x100):
        try:
            table[code] = bytes([code]).decode(codec)
        except UnicodeDecodeError:
            if undefined is not None:
                table[code] = undefined
    if overrides:
        table.update(overrides)
    return table


# WinAnsiEncoding is cp1252. The PDF specification adds one rule the codec has
# no notion of: every code above 40 (octal) that cp1252 leaves undefined shows
# the bullet glyph.
WIN_ANSI_ENCODING = _codec_encoding("cp1252", undefined="\u2022")

# MacRomanEncoding is Mac OS Roman with two PDF-specific slots: 0xDB is
# "currency" (the codec has the later Euro assignment) and 0xF0 is the
# unencoded Apple logo.
MAC_ROMAN_ENCODING = _codec_encoding(
    "mac_roman", overrides={0xDB: "\u00A4", 0xF0: "\uF8FF"})

# StandardEncoding: ASCII, except 0x27/0x60 which are the typographic quotes,
# plus Adobe's own upper region.
_STANDARD_HIGH = {
    0xA1: "\u00A1", 0xA2: "\u00A2", 0xA3: "\u00A3", 0xA4: "\u2044",
    0xA5: "\u00A5", 0xA6: "\u0192", 0xA7: "\u00A7", 0xA8: "\u00A4",
    0xA9: "'", 0xAA: "\u201C", 0xAB: "\u00AB", 0xAC: "\u2039",
    0xAD: "\u203A", 0xAE: "\uFB01", 0xAF: "\uFB02", 0xB1: "\u2013",
    0xB2: "\u2020", 0xB3: "\u2021", 0xB4: "\u00B7", 0xB6: "\u00B6",
    0xB7: "\u2022", 0xB8: "\u201A", 0xB9: "\u201E", 0xBA: "\u201D",
    0xBB: "\u00BB", 0xBC: "\u2026", 0xBD: "\u2030", 0xBF: "\u00BF",
    0xC1: "\u0060", 0xC2: "\u00B4", 0xC3: "\u02C6", 0xC4: "\u02DC",
    0xC5: "\u00AF", 0xC6: "\u02D8", 0xC7: "\u02D9", 0xC8: "\u00A8",
    0xCA: "\u02DA", 0xCB: "\u00B8", 0xCD: "\u02DD", 0xCE: "\u02DB",
    0xCF: "\u02C7", 0xD0: "\u2014", 0xE1: "\u00C6", 0xE3: "\u00AA",
    0xE8: "\u0141", 0xE9: "\u00D8", 0xEA: "\u0152", 0xEB: "\u00BA",
    0xF1: "\u00E6", 0xF5: "\u0131", 0xF8: "\u0142", 0xF9: "\u00F8",
    0xFA: "\u0153", 0xFB: "\u00DF",
}
STANDARD_ENCODING = {c: chr(c) for c in range(0x20, 0x7F)}
STANDARD_ENCODING[0x27] = "\u2019"
STANDARD_ENCODING[0x60] = "\u2018"
STANDARD_ENCODING.update(_STANDARD_HIGH)


# Glyph names, in code order, for the two encodings whose character values are
# taken from the codecs above. Pairing the two gives the Adobe Glyph List
# subset that ``/Differences`` arrays draw on, without duplicating any
# character values by hand.
_WIN_ANSI_NAMES = """
 space exclam quotedbl numbersign dollar percent ampersand quotesingle
 parenleft parenright asterisk plus comma hyphen period slash
 zero one two three four five six seven eight nine
 colon semicolon less equal greater question at
 A B C D E F G H I J K L M N O P Q R S T U V W X Y Z
 bracketleft backslash bracketright asciicircum underscore grave
 a b c d e f g h i j k l m n o p q r s t u v w x y z
 braceleft bar braceright asciitilde .notdef
 Euro .notdef quotesinglbase florin quotedblbase ellipsis dagger daggerdbl
 circumflex perthousand Scaron guilsinglleft OE .notdef Zcaron .notdef
 .notdef quoteleft quoteright quotedblleft quotedblright bullet endash emdash
 tilde trademark scaron guilsinglright oe .notdef zcaron Ydieresis
 space exclamdown cent sterling currency yen brokenbar section
 dieresis copyright ordfeminine guillemotleft logicalnot hyphen registered macron
 degree plusminus twosuperior threesuperior acute mu paragraph periodcentered
 cedilla onesuperior ordmasculine guillemotright onequarter onehalf threequarters questiondown
 Agrave Aacute Acircumflex Atilde Adieresis Aring AE Ccedilla
 Egrave Eacute Ecircumflex Edieresis Igrave Iacute Icircumflex Idieresis
 Eth Ntilde Ograve Oacute Ocircumflex Otilde Odieresis multiply
 Oslash Ugrave Uacute Ucircumflex Udieresis Yacute Thorn germandbls
 agrave aacute acircumflex atilde adieresis aring ae ccedilla
 egrave eacute ecircumflex edieresis igrave iacute icircumflex idieresis
 eth ntilde ograve oacute ocircumflex otilde odieresis divide
 oslash ugrave uacute ucircumflex udieresis yacute thorn ydieresis
""".split()

_MAC_ROMAN_HIGH_NAMES = """
 Adieresis Aring Ccedilla Eacute Ntilde Odieresis Udieresis aacute
 agrave acircumflex adieresis atilde aring ccedilla eacute egrave
 ecircumflex edieresis iacute igrave icircumflex idieresis ntilde oacute
 ograve ocircumflex odieresis otilde uacute ugrave ucircumflex udieresis
 dagger degree cent sterling section bullet paragraph germandbls
 registered copyright trademark acute dieresis notequal AE Oslash
 infinity plusminus lessequal greaterequal yen mu partialdiff summation
 product pi integral ordfeminine ordmasculine Omega ae oslash
 questiondown exclamdown logicalnot radical florin approxequal Delta guillemotleft
 guillemotright ellipsis space Agrave Atilde Otilde OE oe
 endash emdash quotedblleft quotedblright quoteleft quoteright divide lozenge
 ydieresis Ydieresis fraction currency guilsinglleft guilsinglright fi fl
 daggerdbl periodcentered quotesinglbase quotedblbase perthousand Acircumflex Ecircumflex Aacute
 Edieresis Egrave Iacute Icircumflex Idieresis Igrave Oacute Ocircumflex
 apple Ograve Uacute Ucircumflex Ugrave dotlessi circumflex tilde
 macron breve dotaccent ring cedilla hungarumlaut ogonek caron
""".split()


def _build_glyph_names():
    table = {}
    for i, name in enumerate(_WIN_ANSI_NAMES):
        ch = WIN_ANSI_ENCODING.get(0x20 + i)
        if name != ".notdef" and ch is not None:
            table.setdefault(name, ch)
    for i, name in enumerate(_MAC_ROMAN_HIGH_NAMES):
        ch = MAC_ROMAN_ENCODING.get(0x80 + i)
        if name != ".notdef" and ch is not None:
            table.setdefault(name, ch)
    # Names that live only in StandardEncoding.
    table.setdefault("Lslash", "\u0141")
    table.setdefault("lslash", "\u0142")
    table.setdefault("nbspace", "\u00A0")
    return table


GLYPH_NAME_TO_UNICODE = _build_glyph_names()

BASE_ENCODINGS = {
    "WinAnsiEncoding": WIN_ANSI_ENCODING,
    "MacRomanEncoding": MAC_ROMAN_ENCODING,
    "StandardEncoding": STANDARD_ENCODING,
    "PDFDocEncoding": WIN_ANSI_ENCODING,
    # MacExpertEncoding is an expert-set font encoding; its glyphs have no
    # single Unicode mapping. Standard is the closest safe approximation.
    "MacExpertEncoding": STANDARD_ENCODING,
}

_UNI_NAME_RE = re.compile(r"^uni([0-9A-Fa-f]{4,6})$")
_U_NAME_RE = re.compile(r"^u([0-9A-Fa-f]{4,6})$")


def glyph_name_to_unicode(name):
    """Resolve a PostScript glyph name to text, or ``None``.

    Handles the Adobe Glyph List subset used by the standard encodings, the
    algorithmic ``uniXXXX`` / ``uXXXXX`` forms, single-character names, and
    ``name.variant`` suffixes (``a.sc`` -> ``a``).
    """
    if not name or name == ".notdef":
        return None
    hit = GLYPH_NAME_TO_UNICODE.get(name)
    if hit is not None:
        return hit
    m = _UNI_NAME_RE.match(name)
    if m:
        try:
            return "".join(chr(int(m.group(1)[i:i + 4], 16))
                           for i in range(0, len(m.group(1)), 4))
        except ValueError:
            return None
    m = _U_NAME_RE.match(name)
    if m:
        cp = int(m.group(1), 16)
        return chr(cp) if 0 <= cp <= 0x10FFFF and not 0xD800 <= cp <= 0xDFFF else None
    if "." in name:
        return glyph_name_to_unicode(name.split(".", 1)[0])
    if len(name) == 1:
        return name
    return None


def build_simple_encoding(encoding_obj, resolve):
    """Build ``{code: text}`` for a simple font's ``/Encoding`` entry.

    Returns ``(table, declared)``. ``declared`` is False when the font names no
    encoding at all — such fonts keep using the document-level fallback map,
    because they are the symbolic/legacy faces whose codes mean whatever the
    embedded program says they mean.
    """
    encoding_obj = resolve(encoding_obj)
    if isinstance(encoding_obj, Name):
        base = BASE_ENCODINGS.get(str(encoding_obj))
        if base is None:
            return {}, False
        return dict(base), True
    if isinstance(encoding_obj, dict):
        base_name = resolve(encoding_obj.get("BaseEncoding"))
        base = BASE_ENCODINGS.get(str(base_name)) if isinstance(base_name, Name) else None
        declared = base is not None
        table = dict(base if base is not None else STANDARD_ENCODING)
        diffs = resolve(encoding_obj.get("Differences"))
        if isinstance(diffs, list):
            code = 0
            for item in diffs:
                item = resolve(item)
                if isinstance(item, (int, float)):
                    code = int(item)
                elif isinstance(item, Name):
                    ch = glyph_name_to_unicode(str(item))
                    if ch is not None:
                        table[code] = ch
                    else:
                        table.pop(code, None)
                    code += 1
            declared = True
        return table, declared
    return {}, False


# ═══════════════════════════════════════════════════════════════════════════
#  3. CMaps: code -> CID for composite (Type0) fonts
# ═══════════════════════════════════════════════════════════════════════════

class CMapEncoding:
    """Code-space ranges plus code -> CID mapping of a ``/Encoding`` CMap.

    ``/Identity-H`` / ``/Identity-V`` (and any CMap we cannot read) behave as
    two-byte identity, which is what every Identity-keyed PDF uses; embedded
    CMap streams contribute their real ``codespacerange`` widths and
    ``cidrange`` / ``cidchar`` mappings.
    """

    __slots__ = ("widths", "single", "ranges", "identity")

    def __init__(self, identity=True):
        self.identity = identity
        self.widths = set()
        self.single = {}
        self.ranges = []

    def code_lengths(self):
        return sorted(self.widths) if self.widths else [2]

    def cid_for(self, code):
        if self.identity:
            return code
        cid = self.single.get(code)
        if cid is not None:
            return cid
        for lo, hi, base in self.ranges:
            if lo <= code <= hi:
                return base + (code - lo)
        return code

    def iter_codes(self, data):
        """Yield ``(code, cid, nbytes)`` for a shown string."""
        lengths = self.code_lengths()
        i = 0
        n = len(data)
        while i < n:
            for width in lengths:
                if i + width <= n:
                    code = int.from_bytes(data[i:i + width], "big")
                    if self.identity or width == lengths[-1] or self._in_space(code, width):
                        yield code, self.cid_for(code), width
                        i += width
                        break
            else:
                width = lengths[-1]
                code = int.from_bytes(data[i:i + width].ljust(width, b"\x00"), "big")
                yield code, self.cid_for(code), width
                i += width

    def _in_space(self, code, width):
        return width in self.widths


_CODESPACE_RE = re.compile(
    rb"begincodespacerange(.*?)endcodespacerange", re.DOTALL)
_CIDRANGE_RE = re.compile(rb"begincidrange(.*?)endcidrange", re.DOTALL)
_CIDCHAR_RE = re.compile(rb"begincidchar(.*?)endcidchar", re.DOTALL)
_HEXPAIR_RE = re.compile(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>")
_HEXNUM_RE = re.compile(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(\d+)")
_HEXINT_RE = re.compile(rb"<([0-9A-Fa-f]+)>\s*(\d+)")


def parse_cmap_stream(data):
    """Parse an embedded CMap stream into a :class:`CMapEncoding`."""
    enc = CMapEncoding(identity=False)
    for m in _CODESPACE_RE.finditer(data):
        for p in _HEXPAIR_RE.finditer(m.group(1)):
            enc.widths.add(max(1, len(p.group(1)) // 2))
    for m in _CIDRANGE_RE.finditer(data):
        for p in _HEXNUM_RE.finditer(m.group(1)):
            lo = int(p.group(1), 16)
            hi = int(p.group(2), 16)
            enc.ranges.append((lo, hi, int(p.group(3))))
    for m in _CIDCHAR_RE.finditer(data):
        for p in _HEXINT_RE.finditer(m.group(1)):
            enc.single[int(p.group(1), 16)] = int(p.group(2))
    if not enc.widths:
        enc.widths.add(2)
    if not enc.ranges and not enc.single:
        enc.identity = True
    return enc


# ═══════════════════════════════════════════════════════════════════════════
#  4. Document-level decoding strategy + per-resource decoders
# ═══════════════════════════════════════════════════════════════════════════

# Sentinel meaning "the content-stream code IS the Unicode code point".
# Used for Identity-H / direct encoding (most Burmese PDFs — Zawgyi-One and
# modern Unicode fonts alike). Decoding then needs no font and no CMap.
IDENTITY = object()


class CmapIdentityMap(dict):
    """Font-cmap glyph map with the two decode truths for these books:

    - code 0x0000 is the .notdef glyph = tab/space -> decode as a space;
    - an unmapped code that is a valid code point comes from the few fonts
      that encode with EXACT Unicode code points -> decode as chr(code).

    Every other unmapped code stays a [XXXX] placeholder (real gap).
    """


# ---------------------------------------------------------------------------
# Legacy (pre-Unicode) Myanmar layouts
# ---------------------------------------------------------------------------
# These faces predate Unicode Myanmar: the producer typed on an ASCII keyboard
# and the font simply drew Myanmar shapes in the Latin slots, so byte 110 ('n')
# paints ``န``.  Nothing in the PDF records that fact, so a code page has to
# supply it.
#
# A code page is keyed by the byte of the ORIGINAL font, not by a subset's
# private numbering.  That distinction is the whole point: a PDF embeds many
# independently renumbered subsets of one face, and a table keyed to one
# subset is wrong for all the others.  The bridge from a subset's codes back
# to these source bytes is the subset's own /ToUnicode map (see
# ``FontDecoder._legacy_lookup``).
#
# This is the same category of data as WinAnsiEncoding: a reusable property of
# a font layout, valid for any document that uses it.  Several unrelated
# typefaces share one layout (Arlarwade and Amyanmar have no glyph outlines in
# common yet agree on ~80% of this table), because they implement the same
# keyboard, so the registry stays small.
AYAR_LEGACY_CODE_PAGE = {
    32: ' ', 34: '\u104b', 35: '\u100d', 36: '\u100e', 39: '\u104a', 40: '(',
    41: ')', 42: '\u100f', 45: '\u2014', 46: '\u1037', 48: '\u1040', 49: '\u1041',
    50: '\u1042', 51: '\u1043', 52: '\u1044', 53: '\u1045', 54: '\u1046', 55: '\u101b',
    56: '\u1048', 57: '\u1049', 59: '\u1038', 62: '\u1037', 64: '\u100c', 65: '\u1021',
    66: '\u1018', 68: '\u101d', 71: '\u1003', 74: '\u1015', 75: '\u1001', 77: '\u1036',
    78: '\u1014', 79: '\u103e\u102f', 80: '\u1016', 82: '\u101b', 83: '\u1006', 84: '\u1011',
    85: '\u1025', 87: '\u103d\u103e', 88: '\u103d', 90: '\u1008', 92: '\u103a', 94: '\u102e',
    96: '\u1017', 97: '\u102c', 98: '\u1017', 99: '\u1004', 100: '\u1012', 101: '\u1031',
    102: '\u104f', 103: '\u1002', 105: '\u102d', 106: '\u104d', 107: '\u1000', 108: '\u101c',
    109: '\u1019', 110: '\u1014', 111: '\u102f', 112: '\u1015', 113: '\u101e', 114: '\u101b',
    115: '\u1005', 116: '\u1010', 117: '\u102f', 118: '\u100a', 119: '\u1040', 120: '\u103e',
    121: '\u101a', 122: '\u1007', 165: '\u103b', 168: '\u1030', 174: '\u103c', 180: '\u1032',
    181: '\u1039\u1019', 182: '\u1000', 187: '\u1039\u1012', 193: '\u103b\u103d', 194: '\u103c',
    200: '\u1024', 206: '\u101d', 207: '\u1037', 210: '\u104c', 223: '\u101d',
    229: '\u102b', 231: '\u1004\u103a\u1039', 402: '\u1001', 710: '\u1009', 8220: '\u103c',
    8221: '\u103c', 8249: '\u100f\u1039\u100d',

    # ── slots recovered from the document itself ─────────────────────────
    # These bytes were either absent (so they fell through to the
    # document-wide map and decoded as an unrelated letter, a different one
    # per font resource) or mapped to a plain consonant where the face
    # actually draws a STACK. 13,810 of 830,730 lookups on mm-grammar.pdf
    # hit them.
    #
    # Each value was read off the surrounding text and then verified by the
    # word it produces across 445 pages -- the count in the comment is that
    # word's occurrences, which were ZERO before. See
    # .bin/python/derive_codepage.py (marks each unknown slot, learns a
    # character model from the same document, scores every candidate) and
    # audit_codepage.py.
    0x68: '\u101f',              # ဟ    ဟု 780, ဟူသော 690
    0x44: '\u1013',              # ဓ    သာဓက 1164
    0x26: '\u104e',              # ၎    ၎င်း 219
    0x48: '\u103e',              # ှ    မြှုပ် 68
    0x59: '\u103b\u103e',        # ျှ   မည်မျှ 67
    0xc5: '\u102b\u103a',        # ါ်   ခေါ်သည် 441
    0x45: '\u1027',              # ဧ    ဧကဝုစ် 19, ဧရိယာ
    0x49: '\u1023',              # ဣ    ဣတ္ထိလိင် 7
    0x4c: '\u1020',              # ဠ    ပါဠိ 40
    0x51: '\u103f',              # ဿ    ပြဿနာ 5
    0x46: '\u100b', 0x21: '\u100b',   # ဋ   ပိဋကတ် 10
    0xf8: '\u1030',              # ူ    ဖြူ, လှူ
    0x7b: '\u2018', 0x7d: '\u2019',   # the quotation marks before ဟု
    # stacked consonants -- the table had only ္မ and ္ဒ, so 2,672 stacks
    # (64% of every stack in the book) were being flattened or lost
    0xdf: '\u1039\u1005',        # ္စ   ပစ္စည်း 795
    0x7c: '\u1039\u1010',        # ္တ   ကတ္တား 329
    0xee: '\u1039\u1010',        # ္တ   မန္တလေး 83
    0xca: '\u1039\u1011',        # ္ထ   ဣတ္ထိ, စတုတ္ထ, ဝတ္ထု 89
    0xac: '\u1039\u101c',        # ္လ   ပုလ္လိင် 51
    0xce: '\u1039\u1013',        # ္ဓ   သမ္ဗန္ဓ 488
    0x60: '\u1039\u1017',        # ္ဗ   သမ္ဗန္ဓ 488
    8226: '\u100f\u1039\u100f',  # ဏ္ဏ  ဟိရဏ္ဏ, သုဝဏ္ဏ, ပုဏ္ဏား 13

    # ── the book's own notation, not Myanmar letters at all ──────────────
    # Why neither the language model nor the glyph vote could ever find
    # these: both only ever proposed Myanmar characters.
    0x2b: '+',                  # joins word parts: မီးရထား (မီး+ရထား)
    0x3d: '=',                  # term = definition: စားသည်= ပြုခြင်းပြကြိယာ
    0xc9: '_',                  # the fill-in-the-blank rule in the exercises
    0x2f: '/',                  # alternatives: ထိအောင်/တိုင်အောင်

    # ── Pali stacks, several of them TWO letters under one code ──────────
    # The face has a single glyph for a whole cluster, so one byte expands
    # to ပာရာပြိုးတစ်လုံးထက်. Which one it is shows in the base it follows:
    # a byte seen after exactly one consonant is that consonant's subjoined
    # form; a cluster like ္ဌ follows many bases because the Pali word
    # does. Verified by the word each produces (all were 0 before).
    0xa9: '\u1039\u1002',        # ္ဂ    ပုဂ္ဂလ 34, ပုဂ္ဂိုလ် 21, နိဂ္ဂဟိတ်   (only ever after ဂ)
    0xcd: '\u1039\u1006',        # ္ဆ    တိရစ္ဆာန် 20, ဝိစ္ဆာ          (only ever after စ)
    0xa4: '\u100f\u1039\u100c',  # ဏ္ဌ   သဏ္ဌာန် 36, ပုံသဏ္ဌာန် 14   -- two letters, one byte
    0x2122: '\u100b\u1039\u100c',# ဋ္ဌ   အဓိဋ္ဌာန် 3, ပြဋ္ဌာန်း, ဆဋ္ဌမ, ကမ္မဋ္ဌာန်း
    0xab: '\u1039\u1007',        # ္ဇ    ဝိဇ္ဇာ 4, ဝိသဇ္ဇနီ, အာဝဇ္ဇန်း
    0xb1: '\u1039\u1018',        # ္ဘ    ကမ္ဘာ 9
    0x7e: '\u1039\u1014',        # ္န    ပိန္နဲ 2, ကိန္နရာ, ကိန္နရီ
    0xdb: '\u1039\u1008',        # ္ဈ    မဇ္ဈိမ
    0xc7: '\u1004\u103a\u1039',  # င်္   kinzi: အင်္ကျ 4, သူ့အင်္ကျသည်, ပင်နီအင်္ကျ
    0xcc: '\u1039\u1003',        # ္ဃ    အနဂ္ဃ -- the book's own table says ဂ+ဃ, not ဂ+ဂ
    0xf1: '\u1029',              # ဩ     အံ့ဩခြင်း

    # ── the subjoined twin of a consonant that also has a full-size code ──
    # The face carries TWO codes for these letters: the normal one and the
    # stacked one. Both were mapped to the full-size letter, so every
    # doubled cluster came out flat (ကက for က္က). The book's own
    # spelling table is the proof -- it prints rows like "က+က=က္က  သာဓက။
    # တက္ကသိုလ်၊ ဥက္ကဋ္ဌ။", and those rows only read correctly once
    # these three are right.
    0xb6: '\u1039\u1000',        # ္က    တက္ကသိုလ် 6, ဥက္ကဋ္ဌ 4
    0x4a: '\u1039\u1015',        # ္ပ    အဓိပ္ပာယ် 323, ကပ္ပိယ, သပ္ပာယ် 6
    0x192: '\u1039\u1001',       # ္ခ    အက္ခရာ 261, လက္ခဏာ 12, စက္ခု 4, နက္ခတ်
    # a second, heavier pair of quotation marks (headings and cited terms)
    0x3c: '\u2018', 0x2030: '\u2019',   # ‘က၀ဂ်’ဟု အမည်တွင်သည်။
    # 0xE7 (231) above was mapped to the empty string and silently DROPPED
    # 502 times: it is a second kinzi. ဝါစင်္ဂ 116, သင်္ချာ 170.
    0xc4: '\u1039\u1016',        # ္ဖ    ဇာတိပ္ဖိုလ်, သမ္ဖပ္ပလာပ
    # read straight off the book's own spelling table, which prints each
    # stack next to the letters that form it and an example word
    0xa1: '\u100b\u1039\u100b',  # ဋ္ဋ   row "ဋ+ဋ=", example ၀ဋ္ဋ
    0xa3: '\u100d\u1039\u100d',  # ဍ္ဍ   row "ဍ+ဍ=", example ခိဍ္ဍာ
    0xa2: '\u100d\u1039\u100e',  # ဍ္ဎ   row "ဍ+ဎ=", examples ဂုဏဝုဍ္ဎိ, အာယု၀ဍ္ဎန
    0x153: '\u103f',             # ဿ    row "သ+သ=ဿ", ၀ဿာန်, မနုဿ
    0x2dc: '\u102d\u1036',       # ိံ   တာဝတိံ
    0xa7: '\u100f\u1039\u100c',  # ဏ္ဌ   a second code for the cluster: ကဏ္ဌကောစ
    0xd3: '\u1037',              # ့    second code for dot below: နှင့်
    # 0x66 (102) above is \u104f, the genitive particle -- 1,188 of them. It was
    # mapped to the empty string and DROPPED, on the belief that it was a
    # zero-width placeholder glyph. \u1000\u103c\u102d\u101a\u102c\u104f\u1014\u1031\u102c\u1000\u103a, \u1014\u102c\u1019\u103a\u104f\u1021\u1005\u102c\u1038, \u1021\u1005\u102f\u104f\u1021\u1006\u102f\u1036\u1038.
    # Caught by diffing against the book's own text (scripts.db, table
    # mmgrammar): \u104f appeared 67 times there and zero times here.
    # 8249 (0x2039) above is \u100f\u1039\u100d, written directly. It used to map to the
    # ZAWGYI code point U+107A, and the imposter table then turned that into
    # \u1039\u1017 -- correctly, because Zawgyi's U+107A really is \u1039\u1017 (Rabbit agrees).
    # The error was routing an Ayar glyph through a Zawgyi code point at all:
    # in THIS face the glyph is \u100f\u1039\u100d. \u101d\u102b\u1000\u103b\u1000\u100f\u1039\u100d 0 -> 835, \u1015\u100f\u1039\u100d\u102d\u1010 0 -> 1.
    # The imposter table and Rabbit are untouched; \u101e\u1019\u1039\u1017\u1014\u1039\u1013 stays 488.
    # 0x57 (87) above is \u103d+\u103e, not \u1005: \u101b\u103d\u103e\u1031 5 -> 68, \u101b\u103d\u103e\u1031\u1005\u100a\u103a\u1038\u1001\u102f\u1036 3,
    # and the bogus \u101b\u1031\u1005 69 -> 6. 0x73 is the real \u1005 (mapping THAT one
    # to \u103d\u103e destroys \u1005\u102c and every \u1016\u103c\u1005\u103a).
    # 0xC1 (193) above is \u103b+\u103d, not the digit \u1042. Found by scan.py's
    # digit rule: \u1000\u1042\u1014\u103a\u1010\u1031\u102c\u103a \xd760 is \u1000\u103b\u103d\u1014\u103a\u1010\u1031\u102c\u103a. 213 \u1000\u103b\u103d\u1014\u103a\u1010\u1031\u102c\u103a, 59 \u1000\u103b\u103d\u1014\u103a\u1019.
    # 0x32 is the real \u1042 (2,649 of them).
    # 0x4F (79) above is TWO code points, ှ+ု, not ၊. Reading it as a
    # comma hid three very common words completely: မှု 210, ရှု 70,
    # နှိုင်း 85, ညှိနှိုင်း 27 -- all zero before. 0x27 is the real ၊
    # (6,716 of them); mapping THAT one to ှု drops the score to 0.9877.
    # 0x52 (82) above is ရ, not ါ -- the last source of the impossible
    # ံ+် sequence: စက်ရုံ 18, အလုပ်ရုံ 2, လက်ရုံးရည် 3, အမဲရိုး 5.
    # Measured against the alternatives: ရ+ံ gives the same စက်ရုံ but adds
    # a ံ that does not belong (အမဲရိုံး), surviving only because the mark
    # dedup ate the duplicate; ြ puts the impossible-sequence count back
    # to 32 and loses စက်ရုံ entirely.
    # 0xE5 is the real ါ (it carries ပါ, ဝါကျ, ပါဝင်) -- mapping THAT one
    # to ရံ instead destroys 1,941 ပါ.
    0x5f: '',                   # the face's anti-stacking control: it keeps
                                # သသ apart so the pair is not drawn as ဿ
                                # (နာမဝိသေသန). Unicode needs nothing here -- ဿ is its
                                # own character, not a သ+သ ligature -- so it drops.
}

# WinInnwa / "Wwin Burmese" layout, used by the Windows-era Myanmar fonts in
# Praser/fonts/window/. A different keyboard from the Ayar layout above and
# they disagree on nearly every slot, so one table cannot serve both: under
# the Ayar table these documents show ~30% orphan marks, under this one ~2.4%.
WININNWA_LEGACY_CODE_PAGE = {
 0x21:'\u100a',0x22:'\u1013',0x23:'\u103b\u103e',0x24:'\u103d\u103e',0x25:'\u100f',
 0x26:'\u101b',0x27:'\u1012',0x28:'(',0x29:')',0x2a:'\u1002',0x2b:'\u103c',
 0x2c:'\u101a',0x2d:'\u2014',0x2e:'\u104f',0x2f:'\u104a',
 0x30:'\u1040',0x31:'\u1041',0x32:'\u1042',0x33:'\u1043',0x34:'\u1044',
 0x35:'\u1045',0x36:'\u1046',0x37:'\u1047',0x38:'\u1048',0x39:'\u1049',
 0x3a:'\u102b\u103a',0x3b:'\u1038',0x3c:'\u1039\u1013',0x3d:'\u103c',
 0x3e:'\u1039\u1006',0x3f:'\u104b',0x40:'\u1037',
 0x41:'\u1017',0x42:'\u1039\u1018',0x43:'\u1003',0x44:'\u102e',0x45:'\u1014',
 0x46:'\u1004\u103a\u1039',0x47:'\u103d',0x48:'\u1036',0x49:'\u104d',0x4a:'\u1032',
 0x4b:'\u102f',0x4c:'\u1030',0x4d:'\u103c',0x4e:'\u1039\u1012',0x4f:'\u1025',
 0x50:'\u1039\u1005',0x51:'\u1039\u1001',0x52:'\u1039\u1019',0x53:'\u103e',0x54:'\u1024',
 0x55:'\u1039\u1000',0x56:'\u1020',0x57:'\u1039\u1010',0x58:'\u100c',0x59:'\u104c',
 0x5a:'\u1007',0x5b:'\u101f',0x5c:'\u103f',0x5d:'=',0x5e:'\u1009',0x5f:'\u103e\u102f',
 0x60:'\u1039\u1015',
 0x61:'\u1031',0x62:'\u1018',0x63:'\u1001',0x64:'\u102d',0x65:'\u1014',0x66:'\u103a',
 0x67:'\u102b',0x68:'\u1037',0x69:'\u1004',0x6a:'\u103c',0x6b:'\u102f',0x6c:'\u1030',
 0x6d:'\u102c',0x6e:'\u100a',0x6f:'\u101e',0x70:'\u1005',0x71:'\u1006',0x72:'\u1019',
 0x73:'\u103b',0x74:'\u1021',0x75:'\u1000',0x76:'\u101c',0x77:'\u1010',0x78:'\u1011',
 0x79:'\u1015',0x7a:'\u1016',0x7b:'\u1027',0x7c:'\u103b\u103d',0x7d:'\u103c',
 0x7e:'\u1039\u1002',
 0xa1:'[',0xa2:']',0xa4:'$',0xa7:'/',0xaa:'\u103e\u1030',
 0xb1:'\u101b',0xb2:'\u1037',0xb3:'\u1037',0xb4:'\u1039\u1012',0xb5:'%',0xb6:'\u101b',0xb7:'*',
 0xb8:'\u103d\u103e',0xb9:'\u103c',0xba:'\u103c',
 0xbe:'\u100f\u1039\u100d',0xbf:'\u100f\u1039\u100c',0xc0:'\u100f\u1039\u100f',
 0xe6:'\u2026',0xe9:'\u103b\u103d\u103e',0xea:'\u103e',0xeb:'\u103d',
 0xec:'\u103d\u103e',0xed:'\u103b\u103d',0xee:'\u103b\u103d\u103e',0xef:'\u103d\u103e',
 0xf0:'\u103c\u102f',0xf1:'\u103c\u102f',0xf2:'\u103c\u102f',0xf3:'\u103c\u102f',
 0xf4:'\u1004\u103a\u1039\u102e',0xf5:'\u1004\u103a\u1039\u102d',0xf6:'\u102d\u1036',
 0xf8:'\u1004\u103a\u1039\u1036',
}


# layout name -> code page.  Add an entry to support another legacy face.
LEGACY_CODE_PAGES = {
    "ayar": AYAR_LEGACY_CODE_PAGE,
    "wininnwa": WININNWA_LEGACY_CODE_PAGE,
}


class LegacyAyarMap(dict):
    """Marker for a map whose decoded text is in Ayar legacy order."""


def map_code(code_to_uni, cid):
    """Return the Unicode string for a content-stream code, or None if unmapped.

    `code_to_uni` may be:
      - the ``IDENTITY`` sentinel  -> the code is already a code point;
      - a dict of int  -> unicode (from a cmap-derived glyph map); or
      - a dict of int  -> str     (from a ToUnicode CMap, where one code can
        expand to several characters).
    """
    if code_to_uni is IDENTITY:
        if 0 <= cid <= 0x10FFFF and not (0xD800 <= cid <= 0xDFFF):
            return chr(cid)
        return None
    v = code_to_uni.get(cid)
    if v is None:
        if isinstance(code_to_uni, CmapIdentityMap):
            if cid == 0:
                return ' '          # .notdef glyph = tab/space
            if 0x20 <= cid <= 0x10FFFF and not (0xD800 <= cid <= 0xDFFF):
                ch = chr(cid)
                # regex-base validity: only real characters pass — no
                # control, format, surrogate or private-use garbage from
                # stray glyph-ID codes.
                if unicodedata.category(ch)[0] not in 'CC':
                    return ch       # exact-Unicode font, identity decode
        return None
    return chr(v) if isinstance(v, int) else v


def usable_tounicode(mapping):
    """Filter a ``/ToUnicode`` map, or return ``None`` when it carries nothing.

    Copy-protected PDFs ship *blanked* ToUnicode maps (nearly every code maps
    to a space) as decoys. Those are detected (>90% space values) and only
    their non-space entries are kept, so a decoy cannot poison decoding.
    """
    if not mapping:
        return None
    spaces = sum(1 for v in mapping.values() if v == " ")
    if spaces / len(mapping) > 0.9:
        mapping = {k: v for k, v in mapping.items() if v != " "}
    return mapping or None


class DocumentDecoding:
    """The document-wide decoding *fallback*, plus a per-resource decoder cache.

    This is what used to be passed to the extractor as a single map applied to
    every text run. It is still the parser's model-verified truth for the
    symbolic Myanmar faces these books embed, but it is now only ONE input to
    the per-font decoders — fonts that declare a real encoding never touch it.

    ``kind``:
      ``identity``   direct decode, the code is the code point;
      ``cmap``       reversed embedded ``cmap``; the map is keyed by GLYPH ID;
      ``legacy``     legacy character-code map (Arlarwade/Ayar);
      ``tounicode``  merged ``/ToUnicode`` (+ cross-volume sidecar), keyed by code;
      ``none``       nothing available.
    """

    __slots__ = ("kind", "map", "key_kind", "trust_font_tounicode",
                 "legacy_text_fix", "legacy_code_page", "loader", "_decoders",
                 "_page_fonts")

    def __init__(self, kind, mapping=None, legacy_code_page=None):
        self.kind = kind
        self.map = IDENTITY if kind == "identity" else (mapping or {})
        # A reversed font cmap is keyed by glyph id; every other source is
        # keyed by the content-stream code itself.
        self.key_kind = "gid" if kind == "cmap" else "code"
        # 'legacy' and 'cmap' were chosen precisely BECAUSE the document's
        # ToUnicode maps failed verification (copy-protection decoys), and
        # 'identity' means the codes are already code points. In those modes a
        # font's own ToUnicode must not override the verified source.
        self.trust_font_tounicode = kind in ("tounicode", "none")
        self.legacy_text_fix = kind == "legacy"
        # Source-byte -> Unicode table for a legacy layout. When present, a
        # simple font that declares no encoding is decoded through its OWN
        # /ToUnicode into the source font's code space and then through this
        # table, instead of through the single document-wide map.
        self.legacy_code_page = legacy_code_page
        # Filled in by the extractor: one FontResourceLoader per document, so
        # font programs and ToUnicode streams are parsed once, not per page.
        self.loader = None
        self._decoders = {}
        self._page_fonts = {}

    def reset_font_cache(self):
        """Drop cached decoders.

        The caches are keyed by PDF object number, which is only unique
        inside ONE document, so they must be dropped whenever this strategy
        is pointed at a different set of parsed objects.
        """
        self._decoders.clear()
        self._page_fonts.clear()

    def __repr__(self):  # pragma: no cover - debugging aid
        size = "identity" if self.map is IDENTITY else f"{len(self.map)} entries"
        return f"<DocumentDecoding {self.kind} ({size})>"


def as_document_decoding(decoding):
    """Accept either a :class:`DocumentDecoding` or a legacy bare map."""
    if isinstance(decoding, DocumentDecoding):
        return decoding
    if decoding is IDENTITY:
        return DocumentDecoding("identity")
    if isinstance(decoding, LegacyAyarMap):
        return DocumentDecoding("legacy", decoding)
    if isinstance(decoding, CmapIdentityMap):
        return DocumentDecoding("cmap", decoding)
    if not decoding:
        return DocumentDecoding("none")
    # A bare dict from an external caller: treated as a code-keyed map, the
    # same reading a /ToUnicode CMap gets.
    return DocumentDecoding("tounicode", decoding)


class FontResourceLoader:
    """Reads fonts out of a parsed PDF.

    The three stream primitives are injected (see the module docstring) so
    this module never imports :mod:`pdf_extract`.
    """

    def __init__(self, objects, get_stream, parse_ttf_cmap, parse_tounicode_stream):
        self.objects = objects
        self._get_stream = get_stream
        self._parse_ttf_cmap = parse_ttf_cmap
        self._parse_tounicode = parse_tounicode_stream
        self._obj_cache = {}
        self._gid_cache = {}
        self._tu_cache = {}

    # -- object access ----------------------------------------------------
    def object_value(self, ref):
        key = (ref.num, ref.gen)
        if key in self._obj_cache:
            return self._obj_cache[key]
        raw = self.objects.get(key)
        if raw is None and ref.gen != 0:
            raw = self.objects.get((ref.num, 0))
        value = parse_object(raw) if raw else None
        self._obj_cache[key] = value
        return value

    def resolve(self, value, depth=0):
        while isinstance(value, Ref) and depth < 32:
            value = self.object_value(value)
            depth += 1
        return value

    def stream_of(self, ref):
        if not isinstance(ref, Ref):
            return None
        return self._get_stream(self.objects, ref.num, ref.gen)

    # -- font programs ----------------------------------------------------
    def reversed_font_cmap(self, descriptor):
        """``{glyph id: unicode}`` for a descriptor's embedded font program."""
        descriptor = self.resolve(descriptor)
        if not isinstance(descriptor, dict):
            return {}
        for key in ("FontFile2", "FontFile3", "FontFile"):
            ref = descriptor.get(key)
            if not isinstance(ref, Ref):
                continue
            cache_key = (ref.num, ref.gen)
            if cache_key in self._gid_cache:
                return self._gid_cache[cache_key]
            data = self.stream_of(ref)
            table = {}
            if data:
                for uni, gid in (self._parse_ttf_cmap(data) or {}).items():
                    if gid:
                        table.setdefault(gid, uni)
            self._gid_cache[cache_key] = table
            return table
        return {}

    def tounicode_of(self, font_dict):
        """The font's OWN ``/ToUnicode`` map, decoy-filtered (or ``None``)."""
        ref = font_dict.get("ToUnicode")
        if not isinstance(ref, Ref):
            return None
        cache_key = (ref.num, ref.gen)
        if cache_key in self._tu_cache:
            return self._tu_cache[cache_key]
        data = self.stream_of(ref)
        mapping = usable_tounicode(self._parse_tounicode(data)) if data else None
        self._tu_cache[cache_key] = mapping
        return mapping

    def cid_to_gid(self, descendant):
        """Return a callable CID -> GID for a descendant CIDFont."""
        entry = descendant.get("CIDToGIDMap")
        if isinstance(entry, Ref):
            data = self.stream_of(entry)
            if data:
                count = len(data) // 2
                gids = struct.unpack(f">{count}H", data[:count * 2])

                def lookup(cid, _gids=gids):
                    return _gids[cid] if 0 <= cid < len(_gids) else 0
                return lookup
        return None                                  # /Identity (the default)

    # -- page resources ---------------------------------------------------
    def page_font_resources(self, page_num, page_gen=0):
        """``{resource name: font dict}`` for a page.

        Resolves ``/Resources`` and ``/Font`` whether they are inline
        dictionaries or indirect references, and walks the ``/Parent`` chain
        because ``/Resources`` is an inheritable page-tree attribute.
        """
        page = self.resolve(Ref(page_num, page_gen))
        fonts = {}
        seen = set()
        depth = 0
        while isinstance(page, dict) and depth < 32:
            resources = self.resolve(page.get("Resources"))
            if isinstance(resources, dict):
                font_dict = self.resolve(resources.get("Font"))
                if isinstance(font_dict, dict):
                    for name, entry in font_dict.items():
                        if name not in fonts:
                            fonts[name] = entry
            parent = page.get("Parent")
            if not isinstance(parent, Ref) or parent in seen:
                break
            seen.add(parent)
            page = self.resolve(parent)
            depth += 1
        return fonts



def detect_legacy_layout_fonts(objects, get_stream_fn, parse_tounicode_fn):
    """Find simple fonts that paint Myanmar from Latin/ASCII slots.

    Identification is structural, never by name. A subset tag, a rename or an
    outright lie in /BaseFont tells us nothing (this corpus contains the same
    face under two different names), so the test is what the font *is*:

      * a simple font (not Type0),
      * that declares NO /Encoding -- so its codes mean whatever its own
        glyphs mean, and
      * whose /ToUnicode hands back non-Myanmar characters.

    That last point is the giveaway. A well-behaved Myanmar font maps its
    codes to U+1000..U+109F. These map to Latin-1 instead, because the map
    records the byte of the pre-Unicode font the document was typed in, not a
    character. That byte is exactly what a legacy code page is keyed by.

    Returns a list of ``(num, gen)`` references to the fonts found.
    """
    found = []
    for ref, body in objects.items():
        if not isinstance(body, (bytes, bytearray)):
            continue
        if b"/Subtype" not in body or b"/ToUnicode" not in body:
            continue
        if not re.search(rb"/Subtype\s*/(TrueType|Type1|MMType1)\b", bytes(body)):
            continue
        if re.search(rb"/Encoding\s*(/|\d)", bytes(body)):
            continue
        m = re.search(rb"/ToUnicode\s+(\d+)\s+(\d+)\s+R", bytes(body))
        if not m:
            continue
        try:
            raw = get_stream_fn(objects, int(m.group(1)), int(m.group(2)))
            mapping = parse_tounicode_fn(raw) if raw else {}
        except Exception:
            continue
        if not mapping:
            continue
        chars = [ch for v in mapping.values() for ch in v]
        if not chars:
            continue
        myanmar = sum(1 for ch in chars if 0x1000 <= ord(ch) <= 0x109F)
        # Mostly non-Myanmar targets => the map is a bridge, not a translation.
        if myanmar / len(chars) < 0.25:
            found.append(ref)
    return found


class FontDecoder:
    """Decodes shown strings for ONE ``/Font`` resource."""

    __slots__ = ("name", "subtype", "is_cid", "cmap", "to_unicode", "encoding",
                 "has_encoding", "own_gids", "doc", "_cid_to_gid", "base_font")

    def __init__(self, name, subtype="", base_font="", is_cid=False, cmap=None,
                 to_unicode=None, encoding=None, has_encoding=False,
                 own_gids=None, doc=None, cid_to_gid=None):
        self.name = name
        self.subtype = subtype
        self.base_font = base_font
        self.is_cid = is_cid
        self.cmap = cmap or CMapEncoding(identity=True)
        self.to_unicode = to_unicode
        self.encoding = encoding or {}
        self.has_encoding = has_encoding
        self.own_gids = own_gids or {}
        self.doc = doc if doc is not None else DocumentDecoding("none")
        self._cid_to_gid = cid_to_gid

    # -- helpers ----------------------------------------------------------
    @property
    def _trusted_tounicode(self):
        return self.to_unicode if (self.to_unicode and
                                   self.doc.trust_font_tounicode) else None

    def _legacy_lookup(self, code):
        """Decode `code` through this resource's own subset numbering.

        Legacy Myanmar subsets each renumber their glyphs from 0x21 in an
        arbitrary order, so a code means nothing on its own -- two subsets of
        one face disagree on every code they share. Their /ToUnicode map is
        NOT Unicode (it hands back Latin-1 bytes), but it IS a faithful bridge
        back to the code space of the original font, which every subset agrees
        on. Going code -> own /ToUnicode -> layout code page therefore decodes
        each resource with its own numbering instead of borrowing another
        subset's.

        Returns ``None`` when this font cannot be bridged, so the caller can
        fall back to the document-wide map.
        """
        page = self.doc.legacy_code_page
        if not page or not self.to_unicode:
            return None
        src = self.to_unicode.get(code)
        if not src:
            return None
        out = []
        for ch in src:
            hit = page.get(ord(ch))
            if hit is None:
                return None
            out.append(hit)
        return "".join(out)

    def _doc_lookup(self, key):
        """Document-level map lookup, own-font glyph map first."""
        if self.doc.map is IDENTITY:
            return map_code(IDENTITY, key)
        if self.doc.key_kind == "gid" and self.own_gids:
            hit = self.own_gids.get(key)
            if hit is not None:
                return chr(hit) if isinstance(hit, int) else hit
        return map_code(self.doc.map, key)

    def gid_for(self, cid):
        if self._cid_to_gid is None:
            return cid
        return self._cid_to_gid(cid)

    # -- decoding ---------------------------------------------------------
    def decode_code(self, code, cid=None):
        """Decode ONE character code to text (``None`` when unmapped)."""
        tu = self._trusted_tounicode
        if tu is not None:
            hit = tu.get(code)
            if hit is not None:
                return hit
        if not self.is_cid:
            if self.has_encoding:
                # The font DECLARED what its codes mean. Never fall through to
                # the document-level glyph map here: that is exactly how a
                # Latin /WinAnsiEncoding resource used to be re-read as
                # Myanmar glyph ids when it shared a font program with the
                # Myanmar resource.
                return self.encoding.get(code)
            bridged = self._legacy_lookup(code)
            if bridged is not None:
                return bridged
            return self._doc_lookup(code)
        cid = code if cid is None else cid
        key = cid if (self.doc.key_kind == "code" or self.doc.map is IDENTITY) \
            else self.gid_for(cid)
        return self._doc_lookup(key)

    def decode(self, data):
        """Decode a shown string (``bytes``) with this font's rules."""
        if not data:
            return ""
        out = []
        if self.is_cid:
            for code, cid, _width in self.cmap.iter_codes(data):
                text = self.decode_code(code, cid)
                out.append(text if text is not None else f"[{code:04X}]")
            return "".join(out)
        for code in data:
            text = self.decode_code(code)
            if text is None:
                # Unchanged byte — the behaviour simple fonts always had when
                # nothing could decode them; never a placeholder, so no text
                # is lost or invented. Control codes are the one exception:
                # a raw NUL or C1 byte is never readable text, so it is
                # dropped instead of being written into the output.
                text = ("" if (code < 0x20 or 0x7F <= code <= 0x9F)
                              and code not in (0x09, 0x0A, 0x0D) else chr(code))
            out.append(text)
        return "".join(out)


def build_font_decoder(loader, name, entry, doc):
    """Build the :class:`FontDecoder` for one ``/Font`` resource entry."""
    font = loader.resolve(entry)
    if not isinstance(font, dict):
        return FontDecoder(name, doc=doc)
    subtype = str(loader.resolve(font.get("Subtype")) or "")
    base_font = str(loader.resolve(font.get("BaseFont")) or "")
    to_unicode = loader.tounicode_of(font)

    if subtype == "Type0":
        encoding = loader.resolve(font.get("Encoding"))
        if isinstance(encoding, Name):
            cmap = CMapEncoding(identity=True)       # Identity-H/V + predefined
        elif isinstance(font.get("Encoding"), Ref):
            data = loader.stream_of(font.get("Encoding"))
            cmap = parse_cmap_stream(data) if data else CMapEncoding(identity=True)
        else:
            cmap = CMapEncoding(identity=True)
        descendants = loader.resolve(font.get("DescendantFonts"))
        descendant = {}
        if isinstance(descendants, list) and descendants:
            resolved = loader.resolve(descendants[0])
            if isinstance(resolved, dict):
                descendant = resolved
        own_gids = loader.reversed_font_cmap(descendant.get("FontDescriptor"))
        return FontDecoder(
            name, subtype=subtype, base_font=base_font, is_cid=True, cmap=cmap,
            to_unicode=to_unicode, own_gids=own_gids, doc=doc,
            cid_to_gid=loader.cid_to_gid(descendant))

    encoding, declared = build_simple_encoding(font.get("Encoding"), loader.resolve)
    own_gids = loader.reversed_font_cmap(font.get("FontDescriptor"))
    return FontDecoder(
        name, subtype=subtype, base_font=base_font, is_cid=False,
        to_unicode=to_unicode, encoding=encoding, has_encoding=declared,
        own_gids=own_gids, doc=doc)


def page_font_decoders(loader, page_num, page_gen, doc):
    """``{resource name: FontDecoder}`` for a page (cached per font object)."""
    cache_key = (page_num, page_gen)
    cached = doc._page_fonts.get(cache_key)
    if cached is not None:
        return cached
    decoders = {}
    for name, entry in loader.page_font_resources(page_num, page_gen).items():
        key = tuple(entry) if isinstance(entry, Ref) else (page_num, page_gen, name)
        decoder = doc._decoders.get(key)
        if decoder is None:
            decoder = build_font_decoder(loader, name, entry, doc)
            doc._decoders[key] = decoder
        decoders[name] = decoder
    doc._page_fonts[cache_key] = decoders
    return decoders
