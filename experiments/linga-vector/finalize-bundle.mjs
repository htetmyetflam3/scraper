import fs from 'node:fs';import path from 'node:path';import crypto from 'node:crypto';import {execFileSync} from 'node:child_process';
const stage=process.argv[2]||'/tmp/linga-vector-bundle',root=path.join(stage,'Parsed/parser/tools/vector-evaluation');
const zip=path.resolve(process.argv[3]||'release-assets/linga-vector-evaluation-recovery.zip');
const ignored=new Set(['node_modules','.git','.cache']);
function walk(dir){return fs.readdirSync(dir,{withFileTypes:true}).sort((a,b)=>a.name.localeCompare(b.name)).flatMap(e=>ignored.has(e.name)?[]:e.isDirectory()?walk(path.join(dir,e.name)):[path.join(dir,e.name)]);}
const hash=b=>crypto.createHash('sha256').update(b).digest('hex');
const files=walk(root).filter(f=>path.basename(f)!=='MANIFEST.json').map(f=>{const b=fs.readFileSync(f);return {path:path.relative(root,f).split(path.sep).join('/'),bytes:b.length,sha256:hash(b)};});
const manifest={status:'NOT GREEN — recovery evaluation package, no production integration',createdAt:new Date().toISOString(),baseline:'712728395276b4fb57f25b8c3b05757f990a55ca',legacyFonts:'f9cddc495fb107ebd2c95059390dc9083bab7fa9',independentPdfBlob:'383c90b766e7b0929f799eefcbc615766f30889a',recovery:'Old external scratch folders absent. Surviving repo notes/patch/tests preserved; core data recovered and regenerated. See README for unavailable historical diagnostics.',files};
fs.writeFileSync(path.join(root,'MANIFEST.json'),JSON.stringify(manifest,null,2)+'\n');
fs.mkdirSync(path.dirname(zip),{recursive:true});if(fs.existsSync(zip))throw Error('Refusing to replace existing ZIP: '+zip);
const entries=[...files.map(f=>'Parsed/parser/tools/vector-evaluation/'+f.path),'Parsed/parser/tools/vector-evaluation/MANIFEST.json'];
execFileSync('zip',['-q','-9',zip,'-@'],{cwd:stage,input:entries.join('\n')+'\n',maxBuffer:1024*1024});
execFileSync('unzip',['-tq',zip],{stdio:'inherit'});
const bytes=fs.readFileSync(zip),sha=hash(bytes);fs.writeFileSync(path.join(path.dirname(zip),'SHA256SUMS'),`${sha}  ${path.basename(zip)}\n`);
console.log(JSON.stringify({zip,bytes:bytes.length,sha256:sha,payloadFiles:entries.length},null,2));
