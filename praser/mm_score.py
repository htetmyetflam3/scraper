"""Score how much a decoded string reads like real Burmese.

Why this exists
---------------
When a PDF embeds a legacy (pre-Unicode) Myanmar font, its character codes are
meaningless on their own: byte 110 means "draw picture 110", and only a
per-font code page says that picture is ``န``.  When more than one candidate
code page is available the parser must decide which one is right, and nothing
else in the project can tell it:

* the Zawgyi detector answers "Zawgyi or Unicode?" -- every candidate decoding
  is made of Myanmar code points, so they all look alike to it;
* a syllable or word list rejects transliterated names such as ``ရှီယန့်``,
  which biases the score against exactly the text being validated.

So this module asks a structural question instead, using only two facts about
the script:

1. a base character is U+1000..U+1021, U+1025 or U+1027;
2. a dependent mark must attach to a base.

Point 2 is the whole detector.  A vowel sign or tone mark floating with no
base in front of it cannot occur in real Burmese, but a wrong code page
produces them constantly -- it maps source bytes onto the wrong part of the
alphabet, scattering marks where consonants should be.

Measured over a 445-page legacy grammar: wrong decoding, this parser's
corrected decoding, and the publisher's own Unicode text of the same book.

    orphan marks    22.36%   ->   4.46%   ->   0.21%   (ground truth)
    score            0.776       0.955        0.998

Roughly a hundredfold separation, and it does not saturate: the signal stays
sharp right up to correct output, which is what ranking near-miss candidates
needs.

This module NEVER rewrites text.  The ordering rules are used only to measure;
reordering output remains the extractor's job.

Only the *relative* score is meaningful, and only between candidate decodings
of the same source bytes.
"""

from paths import map_json

_MM_DATA = map_json("mm-score")

__all__ = [
    "MARKS", "MARK_ORDER", "is_index", "is_mark", "is_myanmar",
    "orphan_mark_rate", "duplicate_mark_rate", "order_violation_rate",
    "myanmar_ratio", "score_text",
]

# The dependent marks: signs that cannot stand without a base in front of
# them. Membership only -- "is this a mark?" -- which is a different question
# from "which comes first?" (that is MARK_ORDER below). Space and newline are
# NOT marks: they carry a priority in MARK_ORDER but they end a mark run.
# Loaded from the SHARED object dir <root>/map/mm-score.json ("marks") --
# the same file Parser/parsed/text/mm-score.mjs reads.
MARKS = frozenset(_MM_DATA["marks"])


# THE priority table -- the grammar engine's, verbatim, and the only one in
# this codebase. Every reorder and every ordering measurement reads it from
# here (cleanup.reorder_marks imports it as _PRIORITY), so the Python can
# never disagree with the JS engine it mirrors.
#
# U+103A (asat) is 14 and U+1037 (dot below) is 15. Space is 17 and newline
# 18: they close a cluster, so they sort to the end of one.
#
# Any character NOT in this table passes through untouched and terminates the
# current mark run -- U+1039 (virama) is the common case.
# Loaded from <root>/map/mm-score.json ("priority") -- the grammar
# engine's table, one copy for the JS parser and this port alike.
MARK_ORDER = {k: int(v) for k, v in _MM_DATA["priority"].items()}


def is_index(ch):
    """True for a base character that can carry marks."""
    if not ch:
        return False
    cp = ord(ch)
    return (0x1000 <= cp <= 0x1021) or cp == 0x1025 or cp == 0x1027


def is_mark(ch):
    """True for a dependent mark (one that must follow a base)."""
    return ch in MARKS


def is_myanmar(ch):
    """True for any code point in the Myanmar block."""
    return bool(ch) and 0x1000 <= ord(ch) <= 0x109F


def myanmar_ratio(text):
    """Fraction of `text` that is Myanmar code points (0.0 when empty)."""
    if not text:
        return 0.0
    return sum(1 for ch in text if is_myanmar(ch)) / len(text)


def _walk(text):
    """Yield ``(char, attached, run_marks)`` over `text`.

    ``attached`` is True when the character sits in a position where a mark
    would be legal, i.e. directly after a base or after marks already bound to
    one.  ``run_marks`` is the marks collected on the current base so far.
    """
    attached = False
    run = []
    for ch in text:
        mark = is_mark(ch)
        yield ch, attached, run
        if is_index(ch):
            attached = True
            run = []
        elif mark:
            if attached:
                run.append(ch)
        else:
            attached = False
            run = []


def orphan_mark_rate(text):
    """Fraction of dependent marks that follow no base. Lower is better.

    The primary signal. Real Burmese approaches zero; a wrong code page runs
    an order of magnitude higher.
    """
    total = 0
    bad = 0
    for ch, attached, _run in _walk(text):
        if is_mark(ch):
            total += 1
            if not attached:
                bad += 1
    return (bad / total) if total else 0.0


def duplicate_mark_rate(text):
    """Fraction of marks repeating one already on the same base.

    Secondary signal. One base cannot take the same mark twice, so repeats
    indicate a decoding that is emitting noise. Weaker than the orphan rate
    but independent of it, and useful for rejecting a candidate that games the
    orphan rate by producing plausible-looking runs.
    """
    total = 0
    dup = 0
    for ch, attached, run in _walk(text):
        if is_mark(ch) and attached:
            total += 1
            if ch in run:
                dup += 1
    return (dup / total) if total else 0.0


def order_violation_rate(text):
    """Fraction of adjacent mark pairs that appear in an impossible order.

    The strongest signal available, and independent of attachment: a decoding
    can put every mark on a real base and still be wrong if the sequence is
    one Burmese does not permit. Measured over a 445-page legacy grammar:

        garbage decoding              41.86%
        this parser's output          16.27%
        publisher's own Unicode text   0.46%

    Lower is better. Note the parser's 16.27% was invisible to the orphan
    rate, which read a reassuring 4.46% over the same text.
    """
    pairs = 0
    bad = 0
    run = None
    for ch in text:
        if is_index(ch):
            run = []
            continue
        # Membership is MARKS, not MARK_ORDER: the table also carries space
        # (17) and newline (18), and those close a run instead of joining it.
        if ch in MARKS:
            if run is not None:
                run.append(ch)
                if len(run) > 1:
                    pairs += 1
                    if MARK_ORDER[run[-2]] > MARK_ORDER[run[-1]]:
                        bad += 1
            continue
        run = None
    return (bad / pairs) if pairs else 0.0


def score_text(text):
    """Score how much `text` reads like Burmese. No word or syllable list.

    Returns a dict:

      ``score``       ``1 - max(orphan, disorder)``; 1.0 is perfect. The two
                      failures are independent -- a decoding can attach every
                      mark correctly and still sequence them impossibly -- so
                      the worst of the pair decides, and neither can be hidden
                      by the other.
      ``orphan``      fraction of marks with no base in front of them.
      ``disorder``    fraction of mark pairs in an impossible order.
      ``duplicate``   fraction of marks repeated on the same base.
      ``marks``       how many dependent marks were seen. A score over very
                      few marks is not meaningful -- check this before
                      trusting a comparison.

    Compare results only between candidate decodings of the same bytes.
    """
    marks = sum(1 for ch in text if is_mark(ch))
    if not marks:
        return {"score": 0.0, "orphan": 0.0, "disorder": 0.0, "duplicate": 0.0,
                "marks": 0, "myanmar_ratio": myanmar_ratio(text)}
    orphan = orphan_mark_rate(text)
    disorder = order_violation_rate(text)
    return {
        "score": 1.0 - max(orphan, disorder),
        "orphan": orphan,
        "disorder": disorder,
        "duplicate": duplicate_mark_rate(text),
        "marks": marks,
        "myanmar_ratio": myanmar_ratio(text),
    }
