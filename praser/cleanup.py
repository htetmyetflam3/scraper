"""Imposter cleanup + canonical mark reorder.

Runs AFTER detection, once per document decision (CLI: one y/N before
conversion; API: preview highlighted changes, then one apply/skip).

Two steps, with different reach:

``order_text``    normalize (NFC + glue strip) then mark reorder.  ALWAYS,
                  on every path -- Zawgyi (after Rabbit), Unicode, Unknown --
                  and not behind the apply/skip toggle.  There is one
                  canonical mark sequence and it is not a per-document
                  opinion.
``cleanup_text``  the imposter replacement on top of that.  The UNICODE
                  extraction branch ONLY, and only when applied: a Zawgyi
                  page is Rabbit's output, and re-reading its code points as
                  imposters would edit a conversion this module does not own.

The functions only touch characters in their defined sets; everything else
passes through.
"""

import unicodedata

from paths import map_json

_CLEANUP_DATA = map_json("cleanup")

from mm_score import MARK_ORDER as _PRIORITY, is_index

# _PRIORITY is the grammar engine's priority table, imported -- there is
# exactly ONE copy of it in this codebase and it lives in mm_score. Space
# (17) and newline (18) are in it, but they end a mark run rather than join
# one, so reorder_marks below never hoists them.

# Glue: invisible joiners, bidi controls, the dotted circle, the no-break
# space, the combining grapheme joiner, and the bridge tokens the engine uses
# as delimiters. Stripped on the way IN, before imposter cleanup and reorder.
# Loaded from the SHARED object dir <root>/map/cleanup.json ("glue") --
# the same file Parser/parsed/text/cleanup.mjs reads.
_GLUE = frozenset(_CLEANUP_DATA["glue"])

def _is_index(ch: str) -> bool:
    """The engine's ``index()``: is this char a cluster base?

    U+1000..U+1021 plus U+1025 and U+1027, nothing else -- the same test
    ``mm_score.is_index`` makes, so the scorer and the reorder agree on what
    a base is. It is deliberately narrower than "any Myanmar letter": a digit
    or an independent vowel is not a base, so marks after one pass through
    untouched instead of being gathered and sorted onto it.
    """
    return is_index(ch)


# Map of Zawgyi imposters -> correct Unicode codepoints.
# Sorted longest-first so multi-char imposters match before single-char.
# Map of Zawgyi imposters -> correct Unicode codepoints, loaded from
# <root>/map/cleanup.json ("reps") -- the same file the JS parser
# reads. Sorted longest-first so multi-char imposters match before
# single-char, exactly as the embedded literal was.
_IMPOSTER_REPS = sorted(((r[0], r[1]) for r in _CLEANUP_DATA["reps"]),
                        key=lambda pair: len(pair[0]), reverse=True)

# Imposter trie, built once at import, exactly like the engine's: one node per
# character, the replacement hanging off the last node of each key. Matching is
# LONGEST-first, so a multi-character imposter wins over a shorter one that
# starts the same way -- which a flat per-character table cannot express.
_REPLACEMENT = "\0"          # key that cannot collide with a character


def _build_imposter_trie(reps):
    trie = {}
    for frm, to in reps:
        node = trie
        for ch in frm:
            node = node.setdefault(ch, {})
        node[_REPLACEMENT] = to
    return trie


_IMPOSTER_TRIE = _build_imposter_trie(_IMPOSTER_REPS)

# Every key in the table above happens to be a single codepoint today, so the
# whole trie collapses to one ``str.translate`` pass -- and that matters: the
# character-by-character walk is ~20x slower on a 7000-page run. The fast path
# is taken only while that stays true; add a multi-character imposter and the
# trie walk below takes over automatically, with the same result.
_SINGLE_CHAR_ONLY = all(len(frm) == 1 for frm, _ in _IMPOSTER_REPS)
_IMPOSTER_TABLE = (str.maketrans({ord(frm): to for frm, to in _IMPOSTER_REPS})
                   if _SINGLE_CHAR_ONLY else None)


def clean_imposters(t: str) -> str:
    """Replace any lingering Zawgyi imposter sequences with correct Unicode.

    Longest match wins, and a replacement is never re-scanned: the walk
    resumes after what it just emitted, so one imposter cannot cascade into
    another. (``str.translate`` has the same one-pass property, which is why
    the fast path above is equivalent.)
    """
    if not t:
        return t
    if _IMPOSTER_TABLE is not None:
        return t.translate(_IMPOSTER_TABLE)

    chars = list(t)
    n = len(chars)
    out = []
    i = 0
    while i < n:
        node = _IMPOSTER_TRIE
        j = i
        last_match = -1
        replacement = None
        while j < n and chars[j] in node:
            node = node[chars[j]]
            j += 1
            if _REPLACEMENT in node:
                last_match = j
                replacement = node[_REPLACEMENT]
        if replacement is not None:
            out.append(replacement)
            i = last_match
        else:
            out.append(chars[i])
            i += 1
    return "".join(out)


def reorder_marks(t: str) -> str:
    """Reorder Myanmar dependent marks into canonical Unicode order."""
    if not t or not isinstance(t, str):
        return ""

    chars = list(t)
    out = []
    i = 0
    n = len(chars)

    # One loop, no leading-prefix phase: the engine has none either, and it
    # needs none -- a leading space or any other non-base falls through to
    # the pass-through branch at the bottom.
    while i < n:
        ch = chars[i]
        if _is_index(ch):
            out.append(ch)
            i += 1

            # Collect the run. It ends at the next base, or at the first
            # character the priority table does not list (U+1039 virama is
            # the common one) -- that character passes through below. A space
            # (17) and a newline (18) ARE in the table, so they join the run
            # and sort to the end of the cluster, and a second one is dropped
            # by the dedup, exactly as the engine does it.
            marks = []
            seen_priorities = set()
            while i < n:
                nxt = chars[i]
                if _is_index(nxt):
                    break
                p = _PRIORITY.get(nxt)
                if p is None:
                    break
                if p not in seen_priorities:
                    seen_priorities.add(p)
                    marks.append(nxt)
                i += 1

            if marks:
                marks.sort(key=lambda c: _PRIORITY[c])
                out.extend(marks)
        else:
            out.append(ch)
            i += 1

    return "".join(out)


def normalize_text(t: str) -> str:
    """NFC, then drop every glue codepoint.

    The engine's first step. Zero-width joiners, bidi controls, a dotted
    circle placeholder or a stray no-break space sit BETWEEN a base and its
    marks and would otherwise split a cluster in two, so they go before
    anything else looks at the text.
    """
    if not t or not isinstance(t, str):
        return ""
    return "".join(ch for ch in unicodedata.normalize("NFC", t)
                   if ch not in _GLUE)


def order_text(t: str) -> str:
    """Normalize + reorder: the part that runs on EVERY path, always.

    Ordering marks by priority is not an opinion about a document, it is the
    one canonical sequence, so it is not behind the cleanup toggle and not
    tied to a category: Zawgyi (after Rabbit), Unicode and Unknown all get
    it. The glue strip comes with it because a zero-width joiner sitting
    between a base and its marks would otherwise split the cluster and hide
    the very run that needs ordering.

    Imposter replacement is NOT here -- that is the toggleable step, and the
    Unicode path's alone.
    """
    return reorder_marks(normalize_text(t))


def cleanup_text(t: str) -> str:
    """The toggleable step: imposter cleanup, on top of normalize + reorder.

    Reorder runs again after the replacement, because an imposter expands
    into real marks (U+1088 -> U+103E U+102F) that then have to take their
    place in the run.
    """
    return reorder_marks(clean_imposters(normalize_text(t)))
