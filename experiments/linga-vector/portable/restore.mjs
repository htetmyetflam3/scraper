// Unpack the included solid-compressed data. No network requests, no production writes.
import fs from 'node:fs';import path from 'node:path';import crypto from 'node:crypto';import {fileURLToPath} from 'node:url';import {execFileSync} from 'node:child_process';
const root=path.dirname(fileURLToPath(import.meta.url)),payload=path.join(root,'PAYLOAD.tar.xz');
if(fs.existsSync(path.join(root,'work')))throw Error('work/ already exists. Restore into a fresh evaluation directory to avoid replacing prior results.');
const expected=fs.readFileSync(path.join(root,'PAYLOAD.sha256'),'utf8').trim().split(/\s+/)[0];
const actual=crypto.createHash('sha256').update(fs.readFileSync(payload)).digest('hex');if(actual!==expected)throw Error('Payload checksum mismatch');
const names=execFileSync('tar',['-tJf',payload],{encoding:'utf8',maxBuffer:4*1024*1024}).trim().split('\n');
for(const name of names)if(path.isAbsolute(name)||name.split('/').includes('..'))throw Error('Unsafe archive path');
execFileSync('tar',['-xJf',payload,'-C',root,'--no-same-owner'],{stdio:'inherit'});
execFileSync(process.execPath,[path.join(root,'verify.mjs')],{stdio:'inherit'});
console.log('Restored locally. Now run npm ci --no-audit --no-fund, then npm test. Extraction remains NOT GREEN.');
