'use strict';

const path = require('node:path');

const originalWorker = require.resolve('tesseract.js/src/worker-script/node/index.js');
const detectPath = require.resolve('wasm-feature-detect', { paths: [path.dirname(originalWorker)] });
const detect = require(detectPath);



require.cache[detectPath].exports = { ...detect, relaxedSimd: async () => false };

require(originalWorker);
