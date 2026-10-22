/**
 * FILE: Parsed/parser/text/cleanup.mjs
 *
 * Mirror of Praser/Python/modules/cleanup.py. One Python module, one JS
 * module -- this file holds everything cleanup.py holds and nothing else.
 *
 * Imposter cleanup + canonical mark reorder. Runs AFTER detection, once per
 * document decision.
 *
 * Two steps, with different reach:
 *
 *   orderText()    normalize (NFC + glue strip) then mark reorder. ALWAYS, on
 *                  every path -- Zawgyi (after Rabbit), Unicode, Unknown --
 *                  and not behind the apply/skip toggle. There is one
 *                  canonical mark sequence and it is not a per-document
 *                  opinion.
 *   cleanupText()  the imposter replacement on top of that. The UNICODE
 *                  extraction branch ONLY, and only when applied: a Zawgyi
 *                  page is Rabbit's output, and re-reading its code points as
 *                  imposters would edit a conversion this module does not own.
 *
 * The functions only touch characters in their defined sets; everything else
 * passes through.
 *
 * DATA: cleanup.json is generated from cleanup.py by
 * ../tools/gen-parser-data.mjs -- never hand-edit it, and never retype
 * a Python table here.
 *
 * PRIORITY is the grammar engine's priority table, imported from mm-score.mjs
 * exactly as cleanup.py does `from mm_score import MARK_ORDER` -- there is
 * exactly ONE copy of it in this codebase and it lives in the mm_score
 * mirror. Space (17) and newline (18) are in it, but they end a mark run
 * rather than join one, so reorderMarks below never hoists them.
 */
import fs from "node:fs";
import path from "node:path";
import { MAP_DIR } from "../core/paths.mjs";
import { MARK_ORDER as PRIORITY, isIndex } from "./mm-score.mjs";

const DATA = JSON.parse(fs.readFileSync(path.join(MAP_DIR, "cleanup.json"), "utf8"));

/**
 * Glue: invisible joiners, bidi controls, the dotted circle, the no-break
 * space, the combining grapheme joiner, and the bridge tokens the engine uses
 * as delimiters. Stripped on the way IN, before imposter cleanup and reorder.
 */
const GLUE = new Set(DATA.glue);

/**
 * Map of Zawgyi imposters -> correct Unicode code points, LONGEST-first so a
 * multi-character imposter wins over a shorter one that starts the same way
 * -- which a flat per-character table cannot express.
 */
const IMPOSTER_REPS = DATA.reps;

const REPLACEMENT = Symbol("replacement");

function buildImposterTrie(reps) {
  const trie = new Map();
  for (const [from, to] of reps) {
    let node = trie;
    for (const ch of from) {
      if (!node.has(ch)) node.set(ch, new Map());
      node = node.get(ch);
    }
    node.set(REPLACEMENT, to);
  }
  return trie;
}

const IMPOSTER_TRIE = buildImposterTrie(IMPOSTER_REPS);

/**
 * Every key in the table happens to be a single code point today, so the whole
 * trie collapses to one flat lookup -- and that matters: the character-by-
 * character walk is far slower on a long run. The fast path is taken only
 * while that stays true; add a multi-character imposter and the trie walk
 * below takes over automatically, with the same result.
 */
const SINGLE_CHAR_ONLY = IMPOSTER_REPS.every(([from]) => Array.from(from).length === 1);
const IMPOSTER_TABLE = SINGLE_CHAR_ONLY ? new Map(IMPOSTER_REPS) : null;

/**
 * Replace any lingering Zawgyi imposter sequences with correct Unicode.
 *
 * Longest match wins, and a replacement is never re-scanned: the walk resumes
 * after what it just emitted, so one imposter cannot cascade into another.
 * (The flat table has the same one-pass property, which is why the fast path
 * above is equivalent.)
 */
export function cleanImposters(t) {
  if (!t) return t;

  if (IMPOSTER_TABLE !== null) {
    let out = "";
    for (const ch of t) out += IMPOSTER_TABLE.get(ch) ?? ch;
    return out;
  }

  const chars = Array.from(t);
  const n = chars.length;
  const out = [];
  let i = 0;
  while (i < n) {
    let node = IMPOSTER_TRIE;
    let j = i;
    let lastMatch = -1;
    let replacement = null;
    while (j < n && node.has(chars[j])) {
      node = node.get(chars[j]);
      j += 1;
      if (node.has(REPLACEMENT)) {
        lastMatch = j;
        replacement = node.get(REPLACEMENT);
      }
    }
    if (replacement !== null) {
      out.push(replacement);
      i = lastMatch;
    } else {
      out.push(chars[i]);
      i += 1;
    }
  }
  return out.join("");
}

/**
 * The two marks that are written BEFORE their consonant: ေ (U+1031) and ြ
 * (U+103C).
 *
 * Unicode stores a Myanmar syllable in logical order -- consonant first, then
 * its marks -- and leaves it to the renderer to put ေ on the left and wrap ြ
 * around the letter. A shaped PDF is the opposite: its content stream holds
 * glyphs in DRAWING order, so the ra medial and the e vowel really do come
 * first, and decoding those glyphs faithfully yields `ြပိုင်` where Unicode
 * spells `ပြိုင်`. It is not Zawgyi -- every code point is already a correct
 * Unicode one -- it is correct Unicode in the wrong sequence.
 *
 * Only these two are moved. Every other Myanmar mark is stored after its
 * consonant in logical order already, so nothing else can legitimately appear
 * ahead of a base, and a document that is already in logical order is a
 * no-op: with ေ and ြ sitting behind their consonant, nothing here matches.
 */
const PREBASE_MARKS = new Set(["\u1031", "\u103C"]);

/** ျ ြ ွ ှ -- the four medials. */
const MEDIALS = new Set(["\u103B", "\u103C", "\u103D", "\u103E"]);

/**
 * Which consonants each medial may actually follow.
 *
 * Not a guess and not a rule somebody asserted: the set every medial is
 * found with in the syllable inventory itself. Two independent lists agree
 * cell for cell -- MonoSyllabism's 2525 reference syllables and the
 * 2482 in map/burmese-inventory.json -- which is as close to ground truth
 * as this question has.
 *
 *   ျ 26 consonants   ြ 15   ွ 25   ှ 11
 *
 * အ is in none of the four, and neither is လ for ြ: the vowel carrier
 * carries its own vowel, and a medial behind it therefore belongs to the
 * NEXT consonant -- အြကီး is အ + ကြီး, လြေပီ is လေ + ပြီ.
 */
const MEDIAL_HOSTS = {
  "\u103B": "\u1000\u1001\u1002\u1003\u1004\u1005\u1007\u100A\u100B\u100E\u100F\u1010\u1011\u1012\u1013\u1014\u1015\u1016\u1017\u1018\u1019\u101A\u101C\u101D\u101E\u101F",
  "\u103C": "\u1000\u1001\u1002\u1004\u1005\u100A\u1010\u1011\u1012\u1014\u1015\u1016\u1017\u1019\u101A",
  "\u103D": "\u1000\u1001\u1002\u1004\u1005\u1006\u1007\u100A\u100F\u1010\u1011\u1012\u1013\u1014\u1015\u1016\u1017\u1018\u1019\u101A\u101B\u101C\u101D\u101E\u101F",
  "\u103E": "\u1004\u1009\u100A\u100F\u1014\u1019\u101A\u101B\u101C\u101D\u1020",
};

/** May `medial` follow `consonant` at all, per the inventory? */
function medialFollows(medial, consonant) {
  const hosts = MEDIAL_HOSTS[medial];
  return hosts !== undefined && hosts.includes(consonant);
}

/**
 * Does a pre-base mark at `i` open a cluster, or is it already where Unicode
 * puts it?
 *
 * This is the whole difficulty, and getting it wrong edits text that was
 * correct. `သေမှ` and `ေသမှ` differ only in where ေ sits, and in the first one
 * ေ ALREADY belongs to သ: moving it behind မ turns "if I die" into nonsense.
 * A mark is not pre-base because of the character in front of it -- း precedes
                                        `ေ` in ကူးေြပာင်း and so does ြ in ကြေ,
 * and only the first is a cluster start.
 *
 * The test is the canonical order itself. Behind a consonant, or behind a
 * medial that sorts ahead of ေ (ျ ြ ွ ှ, priorities 1-4), a ေ is legal where
 * it stands and is left alone. Behind anything else -- the start of the text,
 * a space, punctuation, or a mark that sorts AFTER ေ such as the visarga in
 * ကူးေ -- it cannot be in logical order, so it opens the next cluster and
 * belongs to the next consonant.
 */
/* The syllable inventory: every syllable the book's own segmentor accepts.
   Loaded once, lazily -- the "walker".

   It VETOES a ေ move, it never CHOOSES one. သေဘာ reads as သေ + ဘာ, and both
   of those are real syllables, whereas သ + ဘော leaves သ as a bare weak
   onset the inventory does not list -- so scoring on the inventory alone
   would keep the WRONG reading and undo the rule this function exists to
   apply. Used only to say "no" it is safe: it refuses a move whose result
   is not a syllable at all, like လေမ်း. */
let INVENTORY = null;
function inventory() {
  if (INVENTORY === null) {
    try {
      const d = JSON.parse(fs.readFileSync(path.join(MAP_DIR, "burmese-inventory.json"), "utf8"));
      INVENTORY = new Set(d.syllables || []);
    } catch {
      INVENTORY = new Set(); // unreadable: the veto never fires
    }
  }
  return INVENTORY;
}

/* Would a ေ landing on the cluster at `j` still spell a real syllable?
   The cluster is its consonant + marks + at most ONE further consonant
   (the final, the မ of လမ်း); a second consonant is the next syllable. */
function candidateIsReal(chars, j) {
  const inv = inventory();
  if (inv.size === 0) return true;
  const parts = [chars[j], "\u1031"];
  let seenConsonant = false;
  for (let k = j + 1; k < chars.length; k += 1) {
    const ch = chars[k];
    if (ch === " " || ch === "\n") break;
    if (isIndex(ch)) {
      if (seenConsonant) break;
      seenConsonant = true;
    }
    parts.push(ch);
  }
  return inv.has(reorderMarks(parts.join("")));
}

function opensCluster(chars, i) {
  let j = i;
  while (j < chars.length && PREBASE_MARKS.has(chars[j])) j += 1;
  if (j >= chars.length || !isIndex(chars[j])) return false; // no consonant to own them

  const prev = i > 0 ? chars[i - 1] : undefined;
  if (prev === undefined) return true;   // start of the text
  /* A medial the consonant in front of it never takes cannot be that
     consonant's, however much the position looks like a home -- it opens
     the next cluster instead.

         အြကီး   is   အ + ကြီး,   not  အြ + ီး
         အြခား   is   အ + ခြား,   not  အြ + ား
         လြေပီ    is   လေ + ပြီ,   not  လြေ + ီ

     Which consonant takes which medial is read off MEDIAL_HOSTS, not
     assumed. Without this the medial stays where the font drew it and each
     of those comes out as an unpronounceable အြ or လြေ, which is what the
     book read like before the outline repair recovered the ြ at all. */
  if (isIndex(prev) && MEDIALS.has(chars[i]) && !medialFollows(chars[i], prev)) return true;
  /* ေ belongs to the consonant BEHIND it, never the one in front.

     This document writes ေ before the consonant it belongs to -- မနေ is
     မ ေ န. So a ေ that turns up behind a consonant cannot be that
     consonant's own: had it been, it would have been written in front of
     it. In ယ ေ န ့ the ေ is န's, and the word is ယနေ့, not ယေန့.

     The old rule read "right behind a consonant: that is its home", which
     is true only of a document already in Unicode order -- and treating
     this one that way left ေ stranded on the wrong consonant all over
     the book. Falling through here sends it to the priority test below,
     where a consonant in front has no rank and so yields. */
  if (isIndex(prev) && chars[i] === "\u1031") {
    /* ...but only into a cluster that can still take a vowel. ေ is a vowel:
       a cluster the killer (်) or the visarga (း) has already closed has no
       room for one, and forcing it in breaks a syllable that was fine.

         သေလမ်း  is  သေ + လမ်း,  not  သ + လေမ်း

       လမ်း ends in ်, so the ေ stays where it is.

       Tried and measured, then removed: a guard that refuses the move when
       the target cluster is already closed, and an inventory check that
       refuses it when ေ would not spell a real syllable. Both fix သေလမ်း,
       and both cost more book-wide than they save -- the first because a
       following closed syllable (က်, း) makes nearly every Burmese cluster
       look closed and cancels the rule outright; the second because
       သေမှ -> သမှေ survives it (မှေ is a real syllable) while 232 genuine
       moves get refused.

       The inventory veto IS kept -- it is the one that never shreds a
       syllable that was already well-formed. It costs 232 syllables
       book-wide (5,044 failing vs 4,812), and သေမှ -> သမှေ survives it
       because မှေ is a real syllable; that case needs a word-level
       reading, not a syllable one. */
    if (!candidateIsReal(chars, j)) return false;
    const rank = PRIORITY.get(prev);
    return rank === undefined ? true : PRIORITY.get(chars[i]) < rank;
  }
  if (isIndex(prev)) return false;       // right behind a consonant: that is its home

  /* Both remaining cases are decided by whether the mark is already in
     canonical order, which is the same question as whether the syllable is
     pronounceable. Canonical order is the priority table: ျ ြ ွ ှ (1-4), then
     ေ (5), then ိ ီ ု ူ (6-9) and ံ ် ့ း (13-16).

       မ ှ ြ ဖ   ြ is 2, ှ is 4: 2 after 4 is out of order, so ြ cannot join
                 မ's cluster -- it opens ဖြစ်. Read the other way it would be
                 မြှ, and the ှ would have swapped places for no reason.
       က ြ ေ     ေ is 5, ြ is 2: in order, so ေ belongs to က. ကြေ is left
                 alone, and သေမှ stays သေမှ instead of becoming သမှေ.
       က ူ း ေ    ေ is 5, း is 16: out of order, and it is also the rule every
                 Myanmar reader knows -- ေ followed by ိ ု ူ or ံ is not a
                 syllable. ေ opens the next cluster: ကူး + ပြောင်း.

     Anything outside the table -- a space, punctuation, a Latin letter --
     ends the cluster in front of it, so the mark opens the next one. */
  const rank = PRIORITY.get(prev);
  if (rank === undefined) return true;
  // The run's own first mark, not ေ's: a run can start with ြ (rank 2), which
  // is out of order after ှ (rank 4) even though ေ (rank 5) would not be.
  return PRIORITY.get(chars[i]) < rank;
}

/** Move ေ and ြ from in front of a consonant to behind it. */
export function movePrebaseMarks(t) {
  if (!t || typeof t !== "string") return "";
  const chars = Array.from(t);
  const out = [];
  let i = 0;
  const n = chars.length;
  while (i < n) {
    const ch = chars[i];
    // The marks lead the CLUSTER, not the line: a shaper emits ကူးပြောင်း as
    // က ူ း ေ ြ ပ ာ င း, with ေ opening the second cluster and therefore
    // arriving behind the first one's visarga. A run of them is read as a
    // unit -- ေြပ is one cluster's leading pair, not two -- and moves to the
    // consonant that closes it.
    if (PREBASE_MARKS.has(ch) && opensCluster(chars, i)) {
      const marks = [];
      while (i < n && PREBASE_MARKS.has(chars[i])) {
        marks.push(chars[i]);
        i += 1;
      }
      out.push(chars[i]); // the consonant they belong to
      i += 1;
      // Behind it, ahead of the marks already there: reorderMarks sorts the
      // whole run into canonical order, so the position only has to be legal.
      out.push(...marks);
      continue;
    }
    out.push(ch);
    i += 1;
  }
  return out.join("");
}

/** Reorder Myanmar dependent marks into canonical Unicode order. */
export function reorderMarks(t) {
  if (!t || typeof t !== "string") return "";

  const chars = Array.from(t);
  const out = [];
  let i = 0;
  const n = chars.length;

  // One loop, no leading-prefix phase: the engine has none either, and it
  // needs none -- a leading space or any other non-base falls through to the
  // pass-through branch at the bottom.
  while (i < n) {
    const ch = chars[i];
    if (isIndex(ch)) {
      out.push(ch);
      i += 1;

      // Collect the run. It ends at the next base, or at the first character
      // the priority table does not list (U+1039 virama is the common one)
      // -- that character passes through below. A space (17) and a newline
      // (18) ARE in the table, so they join the run and sort to the end of
      // the cluster, and a second one is dropped by the dedup, exactly as the
      // engine does it.
      const marks = [];
      const seenPriorities = new Set();
      while (i < n) {
        const nxt = chars[i];
        if (isIndex(nxt)) break;
        const p = PRIORITY.get(nxt);
        if (p === undefined) break;
        if (!seenPriorities.has(p)) {
          seenPriorities.add(p);
          marks.push(nxt);
        }
        i += 1;
      }

      if (marks.length) {
        marks.sort((a, b) => PRIORITY.get(a) - PRIORITY.get(b));
        out.push(...marks);
      }
    } else {
      out.push(ch);
      i += 1;
    }
  }

  return out.join("");
}

/**
 * ZawCode stored-order unmixing — a DIFFERENT function, not the canonical
 * reorder above (named away from reorder/normalize/order on purpose: this
 * codebase already has enough of those). PURE: no NFC, no glue strip, no
 * inventory, no dictionary. It is deliberately destructive on finished
 * Unicode (မြေတော် -> မေတြော်), so it may only run on the exact spans the
 * scanner flags broken — that coordinate gate lives in text/repair.mjs
 * (mendZawCodeText over scan.mjs's scanBrokenSpans). orderText() does not
 * call this.
 *
 * ZawCode (Keymagic "ဇော်ကုဒ်", ZawCode.kms) types ေ/ြ ahead of their
 * consonant. Stored that way, two clusters' marks interleave between two
 * consonants (နေပြီ drawn ေနြပီ; after one ordinary reorder နြေပီ), and
 * priority sort alone can never undo the second form — နြေပီ is already
 * sorted (ြ rank 2 before ေ rank 5). The walk is reorderMarks' own
 * skeleton — pointer reset at every base, the one PRIORITY table
 * (space/newline keep their final ranks and top the pointer out),
 * characters outside the table pass through — with ownership decided by
 * the pointer while marks are collected:
 *
 *   SMALLER ORDER OPENS THE NEXT CLUSTER. A ေ/ြ whose rank is smaller
 *   than the largest rank already seen in this cluster (ေ after း in
 *   ကူးေြပာင်း, ြ after the carried ေ in ေနြပီ) cannot belong here: it and
 *   the pre-base run behind it are carried to the next base and reordered
 *   there. ျ never opens a cluster this way — in ZawCode order ျ keys in
 *   BEHIND its consonant (ကျေ is ေ+က+ျ), so it stays where it lands.
 *
 *   THE SORTED DAMAGED FORM SPLITS AT EMIT. နြေပီ as finished files store
 *   it shows no smaller order (ြ then ေ, ascending). A cluster holding
 *   both ေ and a ြ that arrived behind the base, with another base right
 *   after its marks, gives its medials to that next base instead: ေ stays
 *   with the previous consonant. A ြ that arrived AHEAD of the base with
 *   its own pre-base run (ေြပ in ကူးပြောင်း) is that base's own mark and
 *   never splits; a cluster without ေ keeps the original order untouched
 *   (ခြွင်းချက် reorders by the default rule alone).
 *
 * Pre-base marks with no base in front of them ride the same carry list
 * (ွ in ေြွမ, which movePrebaseMarks cannot take: its run is only {ေ, ြ}).
 */
const ZAWCODE_PREBASE = new Set(["\u1031", "\u103B", "\u103C", "\u103D", "\u103E"]); // ေ ျ ြ ွ ှ

export function unmixZawCodeMarks(t) {
  if (!t || typeof t !== "string") return "";

  const chars = Array.from(t);
  const out = [];
  let carry = [];
  let i = 0;
  const n = chars.length;

  while (i < n) {
    const ch = chars[i];
    if (isIndex(ch)) {
      const base = ch;
      let marks = [...carry];
      carry = [];
      const seenPriorities = new Set(marks.map((m) => PRIORITY.get(m)));
      let pointer = marks.reduce((mx, m) => Math.max(mx, PRIORITY.get(m) ?? 0), 0);
      i += 1;

      // Did a ြ arrive BEHIND this base (not carried into it)? Only that ြ
      // may be split off at emit time below.
      let raBehindBase = false;
      while (i < n) {
        const nxt = chars[i];
        if (isIndex(nxt)) break;
        const p = PRIORITY.get(nxt);
        if (p === undefined) break;

        if ((nxt === "\u1031" || nxt === "\u103C") && p < pointer) {
          while (i < n && ZAWCODE_PREBASE.has(chars[i])) {
            carry.push(chars[i]);
            i += 1;
          }
          break;
        }

        if (nxt === "\u103C") raBehindBase = true;
        if (!seenPriorities.has(p)) {
          seenPriorities.add(p);
          marks.push(nxt);
        }
        if (p > pointer) pointer = p;
        i += 1;
      }

      if (raBehindBase && i < n && isIndex(chars[i]) && marks.includes("\u1031")) {
        carry = marks.filter((m) => MEDIALS.has(m));
        marks = marks.filter((m) => !MEDIALS.has(m));
      }

      marks.sort((a, b) => PRIORITY.get(a) - PRIORITY.get(b));
      out.push(base, ...marks);
    } else if (ZAWCODE_PREBASE.has(ch) && (() => {
      let k = i;
      while (k < n && ZAWCODE_PREBASE.has(chars[k])) k += 1;
      return k < n && isIndex(chars[k]);
    })()) {
      carry.push(ch);
      i += 1;
    } else {
      out.push(...carry);
      carry = [];
      out.push(ch);
      i += 1;
    }
  }

  out.push(...carry);
  return out.join("");
}

/**
 * NFC, then drop every glue code point.
 *
 * The engine's first step. Zero-width joiners, bidi controls, a dotted circle
 * placeholder or a stray no-break space sit BETWEEN a base and its marks and
 * would otherwise split a cluster in two, so they go before anything else
 * looks at the text.
 *
 * Named `normalizeText` to mirror cleanup.py's `normalize_text`. Note that
 * normalize.mjs exports an unrelated function of the same name -- that one is
 * the JS-only pipeline orchestrator and has no Python counterpart. Import
 * this one from this module, explicitly.
 */
export function normalizeText(t) {
  if (!t || typeof t !== "string") return "";
  let out = "";
  for (const ch of t.normalize("NFC")) {
    if (!GLUE.has(ch)) out += ch;
  }
  return out;
}

/**
 * Normalize + reorder: the part that runs on EVERY path, always.
 *
 * Ordering marks by priority is not an opinion about a document, it is the one
 * canonical sequence, so it is not behind the cleanup toggle and not tied to a
 * category: Zawgyi (after Rabbit), Unicode and Unknown all get it. The glue
 * strip comes with it because a zero-width joiner sitting between a base and
 * its marks would otherwise split the cluster and hide the very run that needs
 * ordering.
 *
 * Imposter replacement is NOT here -- that is the toggleable step, and the
 * Unicode path's alone.
 */
export function orderText(t) {
  return dropImpossibleMarks(reorderMarks(movePrebaseMarks(normalizeText(t))));
}

/**
 * The toggleable step: imposter cleanup, on top of normalize + reorder.
 *
 * Reorder runs again after the replacement, because an imposter expands into
 * real marks (U+1088 -> U+103E U+102F) that then have to take their place in
 * the run.
 */
export function cleanupText(t) {
  return dropImpossibleMarks(reorderMarks(cleanImposters(normalizeText(t))));
}

/**
 * A vowel sign ေ cannot take a second one.
 *
 *   ေ + ိ / ု / ူ / ံ  is not a sequence Burmese can spell.
 *
 * `opensCluster` uses that fact to decide where ေ belongs -- it is why
 * ကူးေြပာင်း is ကူး + ပြောင်း and not ကူးေ -- but it can only MOVE ေ, and not
 * every violation is a misplaced one. Some are a glyph that carries too much.
 *
 * In this book, gid 0x2FA of the MyanmarText subsets maps, in the font's own
 * /ToUnicode, to the four code points ြ + ိ + ု + ့. What the page draws there
 * is a ့. The font keeps no GSUB, so nothing contradicts the entry, and the
 * decoder has to believe it: `တွေ့` came out as `တြွေို့`, and with it 125
 * places in the last fifth of the book -- တြွေို့ဆို, တြွေို့မြင်, တြွေို့ဖူး,
 * every one of them an unpronounceable ေ followed by ိ.
 *
 * The repair is to drop the vowel signs that cannot be there. ေ is kept,
 * because a ေ that survives this far was decoded from its own glyph and is
 * attested twice over -- once by the font's cmap, once by the fact that a
 * cluster needs a vowel. The extra signs come from the entry that said too
 * much, and they are the part with no witness of their own.
 *
 * Runs only after the canonical sort: that is what puts the signs in the order
 * the rule is written in, immediately after ေ.
 */
const AFTER_E_IMPOSSIBLE = new Set([
  "\u102D", // ိ
  "\u102F", // ု
  "\u1030", // ူ
  "\u1036", // ံ
]);

export function dropImpossibleMarks(t) {
  if (!t || typeof t !== "string") return "";
  const chars = Array.from(t);
  const out = [];
  for (let i = 0; i < chars.length; i += 1) {
    const ch = chars[i];
    out.push(ch);
    if (ch !== "\u1031") continue; // ေ
    let j = i + 1;
    while (j < chars.length && AFTER_E_IMPOSSIBLE.has(chars[j])) j += 1;
    i = j - 1;
  }
  return out.join("");
}
