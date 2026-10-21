// FILE: main/grapheme/reverse.js
/**
 * walkReverse — lookup in grouped RTL tree with action instruction.
 * 
 * @param {object} reverseTree — grouped rtl JSON (St-1, St-2, ...)
 * @param {string} syllable — full accumulated syllable (root + body)
 * @param {string} rootTok — root character
 * @returns {object} { action: 'continue'|'emit'|'retry'|'fail', id: string|null, cut?: number }
 */
export function walkReverse(reverseTree, syllable, rootTok) {
  const body = syllable.slice(1);
  if (!body) {
    return { action: 'continue', id: null };
  }

  const reversedBody = Array.from(body).reverse().join('');
  const stepKey = `St-${reversedBody.length}`;

  const stepData = reverseTree[stepKey];
  if (!stepData) {
    return { action: 'continue', id: null };
  }

  const entry = stepData[reversedBody];
  if (!entry) {
    return { action: 'continue', id: null };
  }

  for (const [suffixId, roots] of Object.entries(entry)) {
    if (roots.includes(rootTok)) {
      return { action: 'continue', id: `${rootTok}${suffixId}` };
    }
  }

  return { action: 'continue', id: null };
}
