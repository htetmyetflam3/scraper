// graph/shape.js (ex-builder/_build.js)

export function getStep(rootNode, stepNum) {
  const key = `St-${stepNum}`;
  if (!rootNode[key]) rootNode[key] = [];
  return rootNode[key];
}

// POS-tree step shapers (moved from disassembler.js buildPosTree — were nested)
export function ensureNode(tree, root, posKey) {
  if (!tree[root]) tree[root] = {};
  if (!Array.isArray(tree[root][posKey])) tree[root][posKey] = [];
  return tree[root];
}

export function ensureStep(node, n) {
  const key = `St-${n}`;
  if (!node[key]) node[key] = {};
  return node[key];
}

export function addPos(arr, pos) {
  if (!arr.includes(pos)) {
    arr.push(pos);
    return true;
  }
  return false;
}
