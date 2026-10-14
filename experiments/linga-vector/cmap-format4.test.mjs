// Run with: LINGA_ROOT=/path/to/linga node --test experiments/linga-vector/cmap-format4.test.mjs
// Exercise the supplied one-line patch in memory; never modify the checkout.
import fs from 'node:fs';import path from 'node:path';import {pathToFileURL} from 'node:url';
import test from 'node:test';import assert from 'node:assert/strict';
const root=process.env.LINGA_ROOT||'/home/user/linga';
let source=fs.readFileSync(path.join(root,'Parsed/parser/core/ttf-cmap.mjs'),'utf8');
assert.ok(source.includes('const addr = ro + idx * 2;')||source.includes('const addr = ro + (segCount + idx) * 2;'),'Unsupported reader version');
source=source.replace('const addr = ro + idx * 2;','const addr = ro + (segCount + idx) * 2;').replace('"./pylib.mjs"',JSON.stringify(pathToFileURL(path.join(root,'Parsed/parser/core/pylib.mjs')).href));
const {parseTtfCmap}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
function wrap(sub){const cmap=Buffer.alloc(12+sub.length);cmap.writeUInt16BE(1,2);cmap.writeUInt16BE(3,4);cmap.writeUInt16BE(1,6);cmap.writeUInt32BE(12,8);sub.copy(cmap,12);const b=Buffer.alloc(28+cmap.length);b.writeUInt32BE(0x10000);b.writeUInt16BE(1,4);b.write('cmap',12);b.writeUInt32BE(28,20);b.writeUInt32BE(cmap.length,24);cmap.copy(b,28);return b;}
function fmt4({gid=7,delta=0,range=4,code=65}={}){const s=Buffer.alloc(34);s.writeUInt16BE(4);s.writeUInt16BE(34,2);s.writeUInt16BE(4,6);s.writeUInt16BE(code,14);s.writeUInt16BE(65535,16);s.writeUInt16BE(code,20);s.writeUInt16BE(65535,22);s.writeInt16BE(delta,24);s.writeInt16BE(1,26);s.writeUInt16BE(range,28);s.writeUInt16BE(gid,32);return wrap(s);}
test('format-4 range is relative to its own idRangeOffset field: A -> GID 7, not 4',()=>assert.equal(parseTtfCmap(fmt4()).get(65),7));
test('nonzero GID gets delta; zero GID does not',()=>{assert.equal(parseTtfCmap(fmt4({delta:3})).get(65),10);assert.equal(parseTtfCmap(fmt4({gid:0,delta:3})).has(65),false);});
test('zero range still uses the original direct delta branch',()=>assert.equal(parseTtfCmap(fmt4({range:0,delta:-60})).get(65),5));
test('out-of-bounds range is not read',()=>assert.equal(parseTtfCmap(fmt4({range:600})).has(65),false));
test('symbol/private-use codepoints stay explicit in the cmap reader',()=>{const m=parseTtfCmap(fmt4({code:0xf041}));assert.equal(m.get(0xf041),7);assert.equal(m.has(65),false);});
test('format 0, 6 and 12 unaffected',()=>{const a=Buffer.alloc(262);a.writeUInt16BE(262,2);a[6+65]=9;assert.equal(parseTtfCmap(wrap(a)).get(65),9);const s=Buffer.alloc(12);s.writeUInt16BE(6);s.writeUInt16BE(12,2);s.writeUInt16BE(65,6);s.writeUInt16BE(1,8);s.writeUInt16BE(12,10);assert.equal(parseTtfCmap(wrap(s)).get(65),12);const t=Buffer.alloc(28);t.writeUInt16BE(12);t.writeUInt32BE(28,4);t.writeUInt32BE(1,12);t.writeUInt32BE(0x1f600,16);t.writeUInt32BE(0x1f601,20);t.writeUInt32BE(9,24);assert.equal(parseTtfCmap(wrap(t)).get(0x1f600),9);assert.equal(parseTtfCmap(wrap(t)).get(0x1f601),10);});
