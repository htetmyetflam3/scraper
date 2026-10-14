// Rebuild both stages; retain old control-polygon identity verification.
import fs from 'node:fs';import path from 'node:path';import crypto from 'node:crypto';import * as hb from 'harfbuzzjs';
import {TtfFont,glyphContours} from '../snapshot/linga/Parsed/parser/read/glyf.mjs';
import {parsePathData} from '../snapshot/linga/Parsed/parser/shape/svg.mjs';
import {glyphSignature} from '../snapshot/linga/Parsed/parser/shape/contour.mjs';
import {parseTtfCmap} from './ttf-cmap-fixed.mjs';import {HOME,OUT,ROOT,LEGACY} from './paths.mjs';
const raw=JSON.parse(fs.readFileSync(`${ROOT}/Parsed/parser/map/vector/font-witnesses.json`,'utf8'));
const walk=d=>fs.readdirSync(d,{withFileTypes:true}).flatMap(e=>e.isDirectory()?walk(path.join(d,e.name)):[path.join(d,e.name)]);
const files=[...walk(LEGACY),...walk(`${ROOT}/Parsed/fonts`)].filter(f=>/\.[ot]tf$/i.test(f));
const normalize=n=>path.basename(n).replace(/\s*\(\d+\)/g,'').replace(/[^a-z0-9]/gi,'').toLowerCase();
function pack(s){const values=s.points.flatMap(c=>c.flatMap(([x,y])=>[Math.max(0,Math.min(1000,Math.round(x*1000))),Math.max(0,Math.min(1000,Math.round(y*1000)))]));return {c:s.count,r:Math.round(s.aspect*1000),a:s.areas.map(v=>Math.round(v*1000)),p:Buffer.from(new Uint16Array(values).buffer).toString('base64')};}
function symbol(b){for(let i=0;i<b.readUInt16BE(4);i++){const t=12+i*16;if(b.toString('ascii',t,t+4)!=='cmap')continue;const o=b.readUInt32BE(t+8);for(let j=0;j<b.readUInt16BE(o+2);j++){const p=o+4+j*8;if(b.readUInt16BE(p)===3&&b.readUInt16BE(p+2)===0)return true;}}return false;}
const axis=[...new Set(raw.faces.flatMap(r=>Object.keys(r.bytes).map(Number)))],audit=[],curveFaces=[],fixedFaces=[],fixedAudit=[];let slots=0,mismatches=0;
for(const rec of raw.faces){
 const candidates=files.filter(f=>normalize(f)===normalize(rec.name));let chosen=null,ttf;
 for(const file of candidates){let font;try{font=new TtfFont(fs.readFileSync(file));}catch{continue;}let ok=true;for(const code of Object.keys(rec.bytes).map(Number)){const cs=glyphContours(font,font.glyphId(code));const sig=glyphSignature(cs.map(c=>c.map(p=>[p.x/font.unitsPerEm,p.y/font.unitsPerEm])),{samples:raw.samples});if(!sig||JSON.stringify(pack(sig))!==JSON.stringify(rec.bytes[code])){ok=false;break;}}if(ok){chosen=file;ttf=font;break;}}
 if(!chosen)throw Error(`Original packed signatures not reproduced: ${rec.name}`);
 const data=fs.readFileSync(chosen),face=new hb.Face(new hb.Blob(data)),font=new hb.Font(face);font.setScale(face.upem,face.upem);
 const sigFor=gid=>glyphSignature(parsePathData(font.glyphToPath(gid)).map(c=>c.map(([x,y])=>[x/face.upem,y/face.upem])),{samples:raw.samples});
 const curve={};for(const code of Object.keys(rec.bytes).map(Number)){const sig=sigFor(ttf.glyphId(code));if(sig)curve[code]=pack(sig);}
 const file=path.relative(HOME,chosen),sha256=crypto.createHash('sha256').update(data).digest('hex');
 curveFaces.push({name:rec.name,bytes:curve});audit.push({name:rec.name,status:'rebuilt',file,sha256,originalSlots:Object.keys(rec.bytes).length,curveSlots:Object.keys(curve).length});
 const map=parseTtfCmap(data),isSymbol=symbol(data),byByte={},bad=[];
 for(const code of axis){const lookup=isSymbol&&code<=255?code+0xf000:code,gid=map.get(lookup)||0,nominal=font.nominalGlyph(lookup)||0;if(gid!==nominal){bad.push({code,reader:gid,harfbuzz:nominal});continue;}if(!gid)continue;const sig=sigFor(gid);if(sig){byByte[code]=pack(sig);slots++;}}
 mismatches+=bad.length;if(Object.keys(byByte).length>=8)fixedFaces.push({name:rec.name,bytes:byByte});
 fixedAudit.push({name:rec.name,file,sha256,encoding:isSymbol?'windows-symbol-F000-byte-alias':'nominal-codepoint',slots:Object.keys(byByte).length,mismatches:bad});
 if(curveFaces.length%50===0)console.log(`Verified and rebuilt ${curveFaces.length} faces`);
}
const meta={generatedAt:new Date().toISOString(),samples:raw.samples};
fs.writeFileSync(`${OUT}/curve-witnesses.json`,JSON.stringify({...meta,_:'Recovered curve-only shadow; old byte lookup retained for comparison',faces:curveFaces}));
fs.writeFileSync(`${OUT}/curve-witness-audit.json`,JSON.stringify({originalFaces:raw.faces.length,rebuiltFaces:curveFaces.length,audit},null,2)+'\n');
fs.writeFileSync(`${OUT}/corrected-witnesses.json`,JSON.stringify({...meta,_:'Evaluation only: corrected cmap + actual curves; each retained lookup agrees with HarfBuzz',faces:fixedFaces}));
fs.writeFileSync(`${OUT}/corrected-witness-audit.json`,JSON.stringify({originalFaces:raw.faces.length,retainedFaces:fixedFaces.length,slots,mismatches,audit:fixedAudit},null,2)+'\n');
console.log(JSON.stringify({originalFaces:raw.faces.length,verifiedFaces:curveFaces.length,retainedFaces:fixedFaces.length,slots,mismatches}));
