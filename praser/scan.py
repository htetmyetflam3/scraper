"""Scan decoded text for the damage a score cannot see.

Every defect found in the Ayar code page during this work was found the same
way, and never by the quality score: a wrong byte usually produces perfectly
legal Myanmar, so ``score_text`` stays flat while the words are nonsense. What
does move is *structure* -- a mark with no base, a pair Burmese cannot write,
a syllable printed three times.

The rules below are exactly the ones that caught real bugs:

``orphan``      a dependent mark with no base in front of it. This is the
                strongest signal in the set. It is what exposed ``0x4F``,
                which was decoding ``ှု`` as a comma: the comma is legal text,
                so nothing else complained, but it left the ``ိ`` of
                ``နှိုင်း`` with nothing to attach to. 210 ``မှု`` and 70
                ``ရှု`` were hiding behind that one byte.
``impossible``  pairs that cannot occur in one syllable. ``ံ``+``်`` exposed
                ``0x2E`` (anusvara written where dot-below belongs, 9,465
                occurrences) and later ``0x52``. ``ဲ``+``ာ`` exposed the
                ``0x58``/``0xB4`` swap -- ``သဲား`` for ``သွား``.
``stray``       a token made only of marks: the faux-bold shadow copies are
                collapsed and their marks survive alone.
``repeat``      the same syllable printed two or three times in a row
                (``ဆေးတစ်ဖုံ်ဖုံ်ဖုံ``) -- the same shadow-collapse artefact.
``digit``       a digit with Myanmar letters on both sides: ``၀``/``ဝ`` share
                a glyph, and a number does not sit inside a word.
``virama``      ``U+1039`` with nothing stacked after it.

Report the CLUSTER, never the bare character. A lone ``ိ`` tells you nothing;
``ိင်းယှဥ်ပြ`` tells you the word is ``နှိုင်းယှဉ်ပြ`` and that two code points
are missing in front of it.

    python3 modules/scan.py parsed.txt -o parsed.scan.txt
"""

from paths import map_json

_SCAN_DATA = map_json("scan")

import os
import re
import sys
import json
import argparse
import collections

from mm_score import is_index, is_mark

__all__ = ["scan_text", "format_report", "load_dictionary", "unknown_words", "RULES"]

# Token boundary: whitespace and the punctuation that cannot sit inside a word.
_SPLIT = re.compile(r'[\s\u104a\u104b()\[\]"\u2018\u2019\u201c\u201d,\.:;?!/=+_\u2014-]+')

_MYANMAR = lambda c: '\u1000' <= c <= '\u109f'          # noqa: E731
_DIGIT = lambda c: '\u1040' <= c <= '\u1049'            # noqa: E731
_LETTER_OR_MARK = lambda c: (is_index(c) or is_mark(c)  # noqa: E731
                             or c == '\u1039' or c in '\u1023\u1024\u1026\u1028\u1029')

# (key, one-line title, pair regex) for the sequences Burmese cannot write
# (pattern, one-line title, why) for the sequences Burmese cannot write,
# loaded from the SHARED object dir <root>/map/scan.json ("impossible")
# -- the same file Parser/parsed/text/scan.mjs compiles its rules from.
_IMPOSSIBLE = [tuple(r) for r in _SCAN_DATA["impossible"]]


# ── optional: judge the words against a real dictionary ──────────────────
def _read_sealed(path):
    """Decrypt ``master.json.enc`` with the project's OWN loader.

    The container is [4-byte "MGR1"][12-byte IV][16-byte GCM tag][AES-256-GCM]
    and the key is sealed inside ``MonoSyllabism/map/secure.js``. We call that
    module through node rather than lifting the key into Python: the key stays
    where its author put it, and this stays a reader.
    """
    import subprocess
    here = os.path.dirname(os.path.abspath(__file__))
    loader = os.path.abspath(os.path.join(
        here, "..", "..", "..", "MonoSyllabism", "map", "secure.js"))
    if not os.path.exists(loader):
        raise RuntimeError(f"sealed map needs {loader}, which is not in this tree")
    script = ("import {readSecureJson} from %s;"
              "process.stdout.write(JSON.stringify(readSecureJson(%s)));"
              % (json.dumps(loader), json.dumps(os.path.abspath(path))))
    out = subprocess.run(["node", "--input-type=module", "-e", script],
                         capture_output=True, check=True).stdout
    return json.loads(out)


_MAP_CACHE = {}


def load_map(path):
    """The whole map, cached. ``.enc`` goes through the project's secure.js."""
    if path in _MAP_CACHE:
        return _MAP_CACHE[path]
    if path.endswith(".enc"):
        data = _read_sealed(path)
    elif path.endswith((".db", ".sqlite", ".sqlite3")):
        import sqlite3
        con = sqlite3.connect(path)
        data = json.loads(con.execute("select data from master_json").fetchone()[0])
    else:
        data = json.load(open(path, encoding="utf-8"))
    _MAP_CACHE[path] = data
    return data


def clause_model(path):
    """``newClause`` from the map: which forms stand alone as their own word.

    ``\u104d \u104c \u104f \u1024 \u102a \u104e\u1004\u103a\u1038 \u1026\u1038`` are words, not affixes. Gluing them onto the
    neighbour is the tokeniser's own error, and it shows up as a long
    unknown compound rather than as damage in the text.
    """
    md = (load_map(path) or {}).get("mapData") or {}
    nc = md.get("newClause") or {}
    return tuple(nc.get("standalone") or ()), tuple(nc.get("stick") or ())


def load_dictionary(path):
    """Word set from a ``master_json`` sqlite table, a JSON file, or a sealed
    ``.enc`` map read through the project's ``secure.js``.

    Entries are stored SYLLABLE-SEPARATED (``\u1000  \u100f\u1039\u100c  \u1000\u1031\u102c  \u1005``), so every
    lookup has to strip the spaces first -- matching on the stored form
    finds nothing. Mark order matters too: this dictionary and the praser
    both write asat before dot below (``103A 1037``), which is the engine's
    order, NOT the order a keyboard usually produces. A lookup that skips
    either step fails silently and looks like a missing word.
    """
    data = load_map(path)

    # mapData is tables (priority, glue, imposters), not vocabulary: walking it
    # would add single characters and punctuation to the word set.
    if isinstance(data, dict):
        resolution = data.get("resolution") or {}
        data = {k: v for k, v in data.items() if k != "mapData"}
    else:
        resolution = {}

    words = set()

    # resolution is keyed BY the form it resolves (\u1015\u1010\u103a\u101c\u102f\u1036\u1038, \u1015\u103c\u102e\u1038\u101c\u103d\u103e\u1004\u103a), and
    # those keys are real words that appear in no word list -- walking only
    # the values would leave every one of them reading as unknown.
    for section in resolution.values():
        if isinstance(section, dict):
            for form in section:
                w = re.sub(r"\s+", "", form)
                if w:
                    words.add(w)

    def walk(x):
        if isinstance(x, str):
            w = re.sub(r"\s+", "", x)
            # "!LSE:<role>.w" and friends are ROLE MARKERS, not vocabulary
            if not w or w.startswith("!"):
                return
            # A real entry may carry its class as a suffix flag -- ဣတ္ထိ.p,
            # ပုလ္လိင်.p (.p pali, .w wibat, .c conjunction). Keeping the flag
            # on the word makes every one of them read as missing.
            w = re.sub(r"\.[wcp]$", "", w)
            if w:
                words.add(w)
        elif isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(data)
    return words


# Letters, marks and digits ONLY. U+104A/U+104B (၊ ။) and U+104C-U+104F
# live in the Myanmar block too, so a naive [\u1000-\u109f]+ glues the
# sentence terminator onto the word -- ဖြစ်သည်။ never matches a dictionary
# entry, every token reads as unknown, and the failed segmentation
# backtracks for minutes.
_MM_RUN = re.compile(r"[\u1000-\u1049\u105a-\u109f]+")
_ALL_DIGITS = re.compile(r"^[\u1040-\u1049]+$")


def _segmentable(token, words, memo, longest=0):
    """True if the token is a dictionary word, or a run of them.

    Burmese writes compounds without spaces, so a token that is not itself
    an entry is usually several entries joined. Greedy longest-match keeps
    the false-alarm rate down; anything that cannot be split at all is a
    genuine suspect.
    """
    if token in words:
        return True
    if token in memo:
        return memo[token]
    memo[token] = False                       # guard against recursion
    top = min(len(token), longest) if longest else len(token)
    for end in range(top, 0, -1):
        head = token[:end]
        if head in words and (end == len(token)
                              or _segmentable(token[end:], words, memo, longest)):
            memo[token] = True
            return True
    return False


def unknown_words(text, words, limit=40, standalone=()):
    """Tokens the dictionary cannot account for, most frequent first.

    ``standalone`` forms are peeled off first: they are words in their own
    right, so a token like ``\u1021\u1019\u103c\u1014\u103a\u1019\u102c\u1014\u102d\u102f\u1004\u103a\u1004\u1036\u104f`` is the word plus ``\u104f``,
    not one long unknown.
    """
    memo = {}
    standalone = tuple(sorted(standalone, key=len, reverse=True))

    def peel(tok):
        changed = True
        while changed and tok:
            changed = False
            for form in standalone:
                if len(tok) > len(form):
                    if tok.startswith(form):
                        tok = tok[len(form):]; changed = True
                    elif tok.endswith(form):
                        tok = tok[:-len(form)]; changed = True
        return tok
    longest = max((len(w) for w in words), default=0)
    counts = collections.Counter()
    first = {}
    total = 0
    for m in _MM_RUN.finditer(text):
        tok = m.group(0)
        if len(tok) < 2 or _ALL_DIGITS.match(tok):
            continue
        total += 1
        if standalone:
            tok_core = peel(tok)
            if not tok_core or _ALL_DIGITS.match(tok_core):
                continue
        else:
            tok_core = tok
        if not _segmentable(tok_core, words, memo, longest):
            counts[tok] += 1
            first.setdefault(tok, m.start())
    return counts, first, total


def _words(text):
    """(token, start index) for every non-empty token."""
    out, pos = [], 0
    for tok in _SPLIT.split(text):
        if not tok:
            continue
        i = text.find(tok, pos)
        if i < 0:
            i = pos
        out.append((tok, i))
        pos = i + len(tok)
    return out


def _cluster_at(text, i, span=9):
    """The word containing position `i` (clusters read; bare marks do not)."""
    a = i
    while a > 0 and not _SPLIT.match(text[a - 1]):
        a -= 1
    b = i
    while b < len(text) and not _SPLIT.match(text[b]):
        b += 1
    return text[a:b][:span * 3] or text[i]


def _line_of(text, i, newlines):
    """1-based line number for a character offset."""
    lo, hi = 0, len(newlines)
    while lo < hi:
        mid = (lo + hi) // 2
        if newlines[mid] < i:
            lo = mid + 1
        else:
            hi = mid
    return lo + 1


# ── the rules ─────────────────────────────────────────────────────────────
# Anything a dependent mark may legally sit on, for SCANNING only --
# deliberately wider than the engine's ``index()``, which governs reordering
# and stops at U+1021 (+U+1025/U+1027).
#   U+1023 U+1024 U+1026 U+1028 U+1029 U+102A  independent vowels
#   U+103F  great sa is a LETTER and takes marks
#   U+1022  rare, included for completeness
_EXTRA_BASES = ''.join(_SCAN_DATA["extraBases"])  # from <root>/map/scan.json

# In this kind of book a dash or a blank rule stands in for 'any consonant':
# the vowel charts print  U+2014 + vowel  ( ---a  ---i  ---u ) and the
# exercises print a rule to fill in. Marks hanging off those are correctly
# placed on a placeholder, not orphaned, and counting them as damage buries
# the real cases -- they were 258 of 478.
_PLACEHOLDER_BASES = ''.join(_SCAN_DATA["placeholderBases"])  # from <root>/map/scan.json


def _is_base(ch):
    """Base for SCANNING, deliberately wider than the engine's ``index()``.

    ``index()`` governs reordering and stops at U+1021 (+U+1025/U+1027). The
    independent vowels DO carry marks in real text -- ``\u1026\u1038`` is ordinary --
    so counting them as bases here removes false orphans without touching
    the reorder rule.
    """
    return is_index(ch) or ch in _EXTRA_BASES or ch in _PLACEHOLDER_BASES


def _find_orphans(text, words):
    attached = False
    for i, ch in enumerate(text):
        if _is_base(ch):
            attached = True
        elif is_mark(ch):
            if not attached:
                yield i
        else:
            attached = False


def _find_impossible(text, words):
    for pat, _title, _why in _IMPOSSIBLE:
        for m in re.finditer(pat, text):
            yield m.start()


def _find_stray(text, words):
    for tok, i in words:
        if len(tok) >= 1 and all(is_mark(c) or c == '\u1039' for c in tok):
            yield i


def _find_repeat(text, words):
    # THREE or more in a row. Two is ordinary Burmese reduplication
    # (\u1019\u103c\u1014\u103a\u1019\u103c\u1014\u103a, \u1021\u101e\u102e\u1038\u101e\u102e\u1038); three is the shadow-collapse signature.
    for m in re.finditer(r'([\u1000-\u109f]{2,4})\1{2,}', text):
        yield m.start()


def _find_digit(text, words):
    for i, ch in enumerate(text):
        if not _DIGIT(ch):
            continue
        before = text[i - 1] if i else ''
        after = text[i + 1] if i + 1 < len(text) else ''
        if before and after and _LETTER_OR_MARK(before) and _LETTER_OR_MARK(after):
            yield i


def _find_virama(text, words):
    for m in re.finditer(r'\u1039(?![\u1000-\u1021])', text):
        yield m.start()


RULES = [
    ("orphan", "a dependent mark with no base in front of it", _find_orphans),
    ("impossible", "a pair Burmese cannot write in one syllable", _find_impossible),
    ("stray", "a token made only of marks (shadow-collapse debris)", _find_stray),
    ("repeat", "the same syllable printed 3+ times back to back", _find_repeat),
    ("digit", "a digit with Myanmar letters on both sides (\u1040 vs \u101d)", _find_digit),
    ("virama", "U+1039 with nothing stacked after it", _find_virama),
]


def scan_text(text, per_rule=25, words=None, standalone=()):
    """Return ``{rule: {"total", "clusters"}}``; clusters are (word, n, line, context).

    ``words`` is the optional DICTIONARY. The token list is ``tokens`` -- the
    two must not share a name: shadowing the dictionary with the token list
    made every word read as unknown and coverage as 0.00%.
    """
    tokens = _words(text)
    newlines = [i for i, c in enumerate(text) if c == '\n']
    report = {}
    for key, _title, finder in RULES:
        hits = list(finder(text, tokens))
        grouped = collections.Counter()
        first = {}
        for i in hits:
            w = _cluster_at(text, i)
            grouped[w] += 1
            first.setdefault(w, i)
        clusters = []
        for w, n in grouped.most_common(per_rule):
            i = first[w]
            ln = _line_of(text, i, newlines)
            a = text.rfind('\n', 0, i) + 1
            b = text.find('\n', i)
            ctx = text[a:b if b > 0 else len(text)].strip()
            clusters.append((w, n, ln, ctx[:110]))
        report[key] = {"total": len(hits), "distinct": len(grouped),
                       "clusters": clusters}
    if words:
        counts, first, total = unknown_words(text, words, standalone=standalone or ())
        clusters = []
        for w, n in counts.most_common(per_rule):
            i = first[w]
            ln = _line_of(text, i, newlines)
            a = text.rfind('\n', 0, i) + 1
            b = text.find('\n', i)
            clusters.append((w, n, ln, text[a:b if b > 0 else len(text)].strip()[:110]))
        report["unknown"] = {"total": sum(counts.values()), "distinct": len(counts),
                             "clusters": clusters, "tokens": total}
    return report


def format_report(report, source="", total_chars=0):
    out = []
    w = 78
    out.append("=" * w)
    out.append(f"praser scan{('  ' + source) if source else ''}")
    if total_chars:
        out.append(f"{total_chars:,} characters")
    out.append("=" * w)
    out.append("")
    out.append(f"{'rule':<12}{'hits':>8}{'distinct':>10}   what it means")
    for key, title, _ in RULES:
        r = report[key]
        out.append(f"{key:<12}{r['total']:>8}{r['distinct']:>10}   {title}")
    if "unknown" in report:
        r = report["unknown"]
        out.append(f"{'unknown':<12}{r['total']:>8}{r['distinct']:>10}   "
                   f"no dictionary entry, and not splittable into entries")
        known = r["tokens"] - r["total"]
        out.append("")
        out.append(f"dictionary coverage: {known:,} of {r['tokens']:,} tokens "
                   f"= {100.0 * known / r['tokens']:.2f}%")
    out.append("")
    sections = [(k, t) for k, t, _ in RULES]
    if "unknown" in report:
        sections.append(("unknown", "no dictionary entry, and not splittable into entries"))
    for key, title in sections:
        r = report[key]
        if not r["total"]:
            continue
        out.append("")
        out.append("-" * w)
        out.append(f"{key.upper()}  --  {title}")
        out.append(f"{r['total']} hits in {r['distinct']} distinct clusters")
        out.append("-" * w)
        for word, n, line, ctx in r["clusters"]:
            out.append(f"  {n:>5}  {word}")
            out.append(f"         line {line}: {ctx}")
    out.append("")
    return "\n".join(out)


def scan_file(path, out_path=None, per_rule=25, dict_path=""):
    text = open(path, encoding="utf-8").read()
    words = load_dictionary(dict_path) if dict_path else None
    standalone = clause_model(dict_path)[0] if dict_path else ()
    report = scan_text(text, per_rule, words, standalone)
    body = format_report(report, source=path, total_chars=len(text))
    if out_path:
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(body)
    return report, body


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("file", help="a parsed .txt produced by the praser")
    ap.add_argument("-o", "--out", default="", help="write the log here (default: <file>.scan.txt)")
    ap.add_argument("-n", "--per-rule", type=int, default=25,
                    help="how many distinct clusters to list per rule")
    ap.add_argument("-d", "--dict", default="",
                    help="a scripts.db (master_json table) or .json word list; "
                         "adds the 'unknown word' rule and a coverage figure")
    args = ap.parse_args()
    out = args.out or (args.file.rsplit(".", 1)[0] + ".scan.txt")
    _report, body = scan_file(args.file, out, args.per_rule, args.dict)
    print(body[:body.index("\n\n\n")] if "\n\n\n" in body else body)
    print(f"[+] full log written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
