import fs from 'node:fs';import path from 'node:path';import crypto from 'node:crypto';import {fileURLToPath} from 'node:url';
const root=path.dirname(fileURLToPath(import.meta.url)),m=JSON.parse(fs.readFileSync(path.join(root,'MANIFEST.json'),'utf8'));let checked=0;
for(const f of m.files){if(path.isAbsolute(f.path)||f.path.split('/').includes('..'))throw Error('Unsafe manifest path');const file=path.join(root,f.path),b=fs.readFileSync(file);if(b.length!==f.bytes||crypto.createHash('sha256').update(b).digest('hex')!==f.sha256)throw Error('Integrity mismatch: '+f.path);checked++;}
console.log(`Verified ${checked} payload files (SHA-256). Status: ${m.status}.`);
