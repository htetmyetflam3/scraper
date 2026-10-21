import { MMap, NewMap } from './sourceMap.js';
const LOWER_A = 'a'.charCodeAt(0);
const LOWER_Z = 'z'.charCodeAt(0);
const UPPER_A = 'A'.charCodeAt(0);
const UPPER_Z = 'Z'.charCodeAt(0);
export function isLowerOrUpperAZ(char) {
  if (typeof char !== 'string' || char.length !== 1) return false;
  const code = char.charCodeAt(0);
  return (
    (code >= LOWER_A && code <= LOWER_Z) || (code >= UPPER_A && code <= UPPER_Z)
  );
}
export function isLowercaseAZ(char) {
  if (typeof char !== 'string' || char.length !== 1) return false;
  const code = char.charCodeAt(0);
  return code >= LOWER_A && code <= LOWER_Z;
}
export function isUppercaseAZ(char) {
  if (typeof char !== 'string' || char.length !== 1) return false;
  const code = char.charCodeAt(0);
  return code >= UPPER_A && code <= UPPER_Z;
}
const MYANMAR_BASE = /[\u1000-\u1021\u1025\u1027\u103F]/;
// eslint-disable-next-line no-unused-vars -- intentional unused variable in test/experimental code
function isMyanmarBase(char) {
  return MYANMAR_BASE.test(char);
}
export function compose(str, asciiToMm) {
  const baseSet = new Set(NewMap.Base);
  return str.replace(
    /([A-Z])\^\$|\$([A-Z])|\$([a-z])|\$([^A-Za-z])|([A-Z])\^/g,
    (match, upperE, upperDollar, lowerDollar, otherDollar, upper) => {
      if (upperE) {
        return upperE + '#';
      }
      if (upperDollar) {
        const asc = upperDollar;
        if (baseSet.has(asc)) {
          return asciiToMm.get(asc) ?? match;
        }
        return match;
      }
      if (lowerDollar) {
        const asc = lowerDollar;
        if (baseSet.has(asc)) {
          return asciiToMm.get(asc) ?? match;
        }
        return match;
      }
      if (otherDollar) {
        const asc = otherDollar;
        if (baseSet.has(asc)) {
          return asciiToMm.get(asc) ?? match;
        }
        return match;
      }
      if (upper) {
        return String.fromCharCode(upper.charCodeAt(0) + 32);
      }
      return match;
    },
  );
}
// eslint-disable-next-line no-unused-vars -- intentional unused variable in test/experimental code
export function decompose(str, mmToAscii, asciiToMm) {
  const myanmarBaseSet = new Set(MMap.Base);
  return withBraceBypass(str, (ch) => {
    if (ch === '#') return '^$';
    if (isLowercaseAZ(ch)) {
      return String.fromCharCode(ch.charCodeAt(0) - 32) + '^';
    }
    if (myanmarBaseSet.has(ch)) {
      return '\u1039' + ch;
    }
    return ch;
  });
}
/**
 * Maps a single Myanmar token to its ASCII equivalent.
 * @param {string} token - Single Myanmar character.
 * @param {Map} mmToAscii - Myanmar→ASCII mapping table.
 * @returns {string|null} ASCII token or null if not mapped.
 */
export function burmeseTokenToAscii(token, mmToAscii) {
  return mmToAscii.get(token) ?? null;
}
/**
 * Maps a single ASCII token to its Myanmar equivalent.
 * @param {string} token - Single ASCII token.
 * @param {Map} asciiToMm - ASCII→Myanmar mapping table.
 * @returns {string|null} Myanmar character or null if not mapped.
 */
export function asciiTokenToBurmese(token, asciiToMm) {
  return asciiToMm.get(token) ?? null;
}
/**
 * Pass-through with brace bypass.
 * Applies transformFn only to characters outside { } braces.
 * Content inside braces is preserved as-is.
 *
 * @param {string} str - Input string.
 * @param {Function} transformFn - Called with (char, index) for each char outside braces.
 * @returns {string} Transformed string with braces preserved.
 */
export function withBraceBypass(str, transformFn) {
  let out = '';
  let i = 0;
  while (i < str.length) {
    const ch = str[i];
    if (ch === '{') {
      const endBrace = str.indexOf('}', i + 1);
      if (endBrace !== -1) {
        out += str.slice(i, endBrace + 1);
        i = endBrace + 1;
        continue;
      }
      out += ch;
      i++;
      continue;
    }
    if (ch === '}') {
      out += ch;
      i++;
      continue;
    }
    out += transformFn(ch, i);
    i++;
  }
  return out;
}

// ─── Number detection (moved from Linga/main/tagger.js) ─────────────

/* Both scripts in one set — ASCII (NewMap.Num) + Burmese (MMap.Num).
   Deliberately NOT ENGLISH_DIGITS (sourceMap.json), which also carries the
   bridge delimiters $ and % — a token made of those must not count as a
   number here, same content the tagger's old ALL_DIGITS carried. */


/**
 * True when a token is a number: at least one digit (either script), with
 * only separators (space - / . ,) besides. Moved verbatim from tagger.js.
 * @param {string} tok
 * @returns {boolean}
 */
export function isNumber(tok) {
  if (!tok) return false;
  let hasDigit = false;
  for (const ch of tok) {
    if (ALL_DIGITS.has(ch)) {
      hasDigit = true;
      continue;
    }
    if (ch === ' ' || ch === '-' || ch === '/' || ch === '.' || ch === ',') continue;
    return false;
  }
  return hasDigit;
}