/* --- main/grapheme/traverser.js --- */
import { getStepKey, findEntry, getTree, getRtlTree, getPosTree } from './Tree.loaded.js';
import { walkReverse } from './reverse.js';
import {
  // eslint-disable-next-line no-unused-vars -- intentional unused variable in test/experimental code
  walkerStart, walkerEnd,
  walkStepHit, walkStepEmpty, walkQuit, walkFail, walkOutputSyllables,
} from '../../../Bridge/logen.js';

export function walk(block, startPos) {
  const tree = getTree();
  const reverseTree = getRtlTree();
  const rootTok = block[startPos];
  const rootNode = tree.Root?.[rootTok];

  if (!rootNode) {
    walkFail('no-root-node');
    return { endPos: startPos + 1, lastValidPos: startPos + 1, id: null };
  }

  walkerStart({ pos: startPos, ch: rootTok });

  let readPos = startPos + 1;
  let step = 1;
  let lastSafePos = startPos + 1;
  let currentId = null;
  let emitSyllable = block[startPos];
  let acc = block[startPos];

  while (true) {
    const stepKey = getStepKey(rootNode, step);
    const stepData = rootNode[stepKey];
    const match = findEntry(stepData, block, readPos);

    if (!match) {
      walkQuit('no-match');
      walkOutputSyllables(emitSyllable ? 1 : 0);
      return { endPos: lastSafePos, lastValidPos: lastSafePos, id: currentId };
    }

    const tok = match.tok;
    readPos += match.len;
    acc += tok;

    const ids = match.entry[tok];
    const hasIds = Array.isArray(ids) && ids.length > 0;

    if (hasIds) {
      const rev = walkReverse(reverseTree, acc, rootTok);
      if (rev.action === 'continue' && rev.id) {
        lastSafePos = readPos;
        currentId = rev.id;
        emitSyllable = acc;
      }
    }

    if (hasIds) {
      walkStepHit(rootTok, step);
    } else {
      walkStepEmpty(rootTok, step);
    }

    if (stepKey === 'St-F') {
      walkQuit('stf');
      walkOutputSyllables(emitSyllable ? 1 : 0);
      return { endPos: lastSafePos, lastValidPos: lastSafePos, id: currentId };
    }

    step++;
  }
}

export function walkPos(block, startPos) {
  const tree = getPosTree();
  const rootTok = block[startPos];
  const rootNode = tree[rootTok];

  if (!rootNode) {
    return { endPos: startPos + 1, lastValidPos: startPos + 1, rawPos: ['?'] };
  }

  let readPos = startPos + 1;
  let step = 1;
  let lastValidPos = startPos + 1;
  let currentPos = Array.isArray(rootNode.x) && rootNode.x.length > 0
    ? rootNode.x
    : ['?'];

  let currentNode = rootNode;

  while (true) {
    const stepKey = `St-${step}`;
    const stepData = currentNode[stepKey];

    if (!stepData || readPos >= block.length) {
      break;
    }

    const nextTok = block[readPos];
    const nextNode = stepData[nextTok];

    if (!nextNode) {
      break;
    }

    readPos++;

    if (Array.isArray(nextNode.x) && nextNode.x.length > 0) {
      lastValidPos = readPos;
      currentPos = nextNode.x;
    }

    currentNode = nextNode;
    step++;
  }

  return {
    endPos: readPos,
    lastValidPos,
    rawPos: currentPos,
  };
}
