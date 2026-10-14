// Build only in a staging directory; live Linga/parser files are never changed.
import fs from 'node:fs';import path from 'node:path';import crypto from 'node:crypto';
import {fileURLToPath} from 'node:url';import {execFileSync} from 'node:child_process';
const here=path.dirname(fileURLToPath(import.meta.url));
const stage=process.argv[2]||'/tmp/linga-vector-bundle';
const source=process.env.LINGA_SOURCE||'/tmp/linga-recovery/htetmyetflam3-linga-7127283';
const legacy=process.env.LINGA_LEGACY||'/tmp/linga-recovery/htetmyetflam3-linga-f9cddc4/Praser/fonts';
const root=path.join(stage,'Parsed/parser/tools/vector-evaluation');
fs.mkdirSync(root,{recursive:true});
fs.cpSync(path.join(here,'portable'),root,{recursive:true});
for(const rel of ['Parsed/parser','Parsed/fonts','logen','package.json']){
 const dest=path.join(root,'snapshot/linga',rel);fs.mkdirSync(path.dirname(dest),{recursive:true});fs.cpSync(path.join(source,rel),dest,{recursive:true});
}
// This unchanged 25 MB OCR model is not used in the digital-vector lane.
fs.rmSync(path.join(root,'snapshot/linga/Parsed/parser/model/mya.traineddata'),{force:true});
fs.mkdirSync(path.join(root,'assets'),{recursive:true});fs.cpSync(legacy,path.join(root,'assets/legacy-fonts'),{recursive:true});
fs.mkdirSync(path.join(root,'patches'),{recursive:true});fs.copyFileSync(path.join(here,'cmap-format4.patch'),path.join(root,'patches/cmap-format4.patch'));
const baseSvg=fs.readFileSync(path.join(source,'Parsed/parser/shape/svg.mjs'),'utf8');
if(baseSvg.split('j = i += 1').length!==2)throw Error('Unexpected nesting loop');
fs.writeFileSync(path.join(root,'work/svg-fixed.mjs'),baseSvg.replace('j = i += 1','j = i, i += 1'));
const cmap=fs.readFileSync(path.join(source,'Parsed/parser/core/ttf-cmap.mjs'),'utf8');
fs.writeFileSync(path.join(root,'work/ttf-cmap-fixed.mjs'),cmap.replace('"./pylib.mjs"','"../snapshot/linga/Parsed/parser/core/pylib.mjs"').replace('const addr = ro + idx * 2;','const addr = ro + (segCount + idx) * 2;'));
const line=baseSvg.split('\n').findIndex(l=>l.includes('j = i += 1')),before=baseSvg.split('\n').slice(line-2,line+3);
let patch='--- a/Parsed/parser/shape/svg.mjs\n+++ b/Parsed/parser/shape/svg.mjs\n'+`@@ -${line-1},5 +${line-1},5 @@\n`;
for(const l of before)patch+=l.includes('j = i += 1')?'-'+l+'\n+'+l.replace('j = i += 1','j = i, i += 1')+'\n':' '+l+'\n';
fs.writeFileSync(path.join(root,'patches/svg-nesting.patch'),patch);
fs.mkdirSync(path.join(root,'notes/packaging'),{recursive:true});
for(const f of ['prepare-bundle.mjs','finalize-bundle.mjs'])fs.copyFileSync(path.join(here,f),path.join(root,'notes/packaging',f));
for(const f of ['HANDOFF.md','README.md'])fs.copyFileSync(path.join(here,f),path.join(root,'notes',f));
const portableTest=fs.readFileSync(path.join(here,'cmap-format4.test.mjs'),'utf8');
fs.writeFileSync(path.join(root,'notes/original-cmap-format4.test.mjs'),portableTest);
fs.writeFileSync(path.join(root,'work/cmap-format4.test.mjs'),portableTest.replace("process.env.LINGA_ROOT||'/home/user/linga'","process.env.LINGA_ROOT||fileURLToPath(new URL('../snapshot/linga',import.meta.url))").replace("import {pathToFileURL} from 'node:url'","import {pathToFileURL,fileURLToPath} from 'node:url'"));
fs.mkdirSync(path.join(root,'fixtures'),{recursive:true});
if(!fs.existsSync(path.join(root,'fixtures/bms.pdf'))){
 const blob=JSON.parse(execFileSync('gh',['api','repos/nchanko/Myanmar-Ebook-OCR/git/blobs/383c90b766e7b0929f799eefcbc615766f30889a'],{encoding:'utf8'}));
 fs.writeFileSync(path.join(root,'fixtures/bms.pdf'),Buffer.from(blob.content,'base64'));
}
const trace=path.join(root,'fixtures/old-tested-trace.svg');
if(!fs.existsSync(trace)){
 const commit=JSON.parse(execFileSync('gh',['api','repos/htetmyetflam3/linga/commits/887a171'],{encoding:'utf8'}));
 const result=JSON.parse(execFileSync('gh',['api',`repos/htetmyetflam3/linga/contents/Parsed/fonts/trace.svg?ref=${commit.parents[0].sha}`],{encoding:'utf8',maxBuffer:2*1024*1024}));
 fs.writeFileSync(trace,Buffer.from(result.content,'base64'));
}
fs.writeFileSync(path.join(root,'fixtures/provenance.json'),JSON.stringify({pdf:{repo:'nchanko/Myanmar-Ebook-OCR',blob:'383c90b766e7b0929f799eefcbc615766f30889a',bytes:fs.statSync(path.join(root,'fixtures/bms.pdf')).size,sha256:crypto.createHash('sha256').update(fs.readFileSync(path.join(root,'fixtures/bms.pdf'))).digest('hex')},trace:{repo:'htetmyetflam3/linga',ref:'887a171^',path:'Parsed/fonts/trace.svg',sha256:crypto.createHash('sha256').update(fs.readFileSync(trace)).digest('hex')}},null,2)+'\n');
console.log(root);
