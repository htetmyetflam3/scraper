// Reconstructed TARGETED regression probes, not blind complete extraction.
import fs from 'node:fs';import crypto from 'node:crypto';import * as hb from 'harfbuzzjs';
import {loadShapes,parsePathData,bboxOf} from '../snapshot/linga/Parsed/parser/shape/svg.mjs';
import {glyphSignature,matchContours,contourDistance} from '../snapshot/linga/Parsed/parser/shape/contour.mjs';
import {loadMap,dictionaryWords,scanText} from '../snapshot/linga/Parsed/parser/text/scan.mjs';
import {repairText} from '../snapshot/linga/Parsed/parser/text/repair.mjs';import {OUT,ROOT} from './paths.mjs';
const V=`${ROOT}/Parsed/parser/map/vector`,F=`${ROOT}/Parsed/fonts/Unicode`;
const rules=JSON.parse(fs.readFileSync(`${V}/shape-overrides.json`,'utf8')),aliases=JSON.parse(fs.readFileSync(`${V}/shape-alias.json`,'utf8')).pairs;
const d=loadMap(`${ROOT}/Parsed/parser/model/master.json.enc`),words=dictionaryWords(d);
const s=fs.readFileSync(`${OUT}/page-input/bms-page1-a4.svg`,'utf8'),geom=JSON.parse(fs.readFileSync(`${OUT}/page-input/bms-page1-a4-geometry.json`,'utf8'));
const paths=[...s.matchAll(/<path data-font="([^"]*)" data-source-code="([^"]*)"[^>]*d="([^"]*)"\/>/g)].map(m=>({font:m[1],code:Number(m[2]),element:m[0],contours:parsePathData(m[3])}));
let world;for(let i=0;i<paths.length-3;i++){const a=paths.slice(i,i+4);if(a.every((p,j)=>p.code===[117,114,199,109][j]&&p.font===a[0].font)){world=a;break;}}
if(!world)throw Error('Problem-word source sequence missing');
const sy=geom.mapping.scale,ty=geom.mapping.ty;
const title=paths.filter(p=>p.contours.length).filter(p=>{const b=bboxOf(p.contours.flat());return b.cx>260*sy&&b.cx<360*sy&&b.cy>78*sy+ty&&b.cy<102*sy+ty;});
const tasks=[{name:'world-word',query:world,labels:['က','လ','ဿ','ယ'].flatMap(base=>['္ဘ','္က','္လ'].map(stack=>base+'မ'+stack+'ာ'))},{name:'title',query:title,labels:['့','၌','ဌ','ွ'].flatMap(a=>['့','၌','ဌ','ွ'].map(b=>'လူ'+a+'အခွင်'+b+'အရေး'))}];
function differingUnits(a,b){let first=0;while(first<a.length&&first<b.length&&a[first]===b[first])first++;let end=0;while(end<a.length-first&&end<b.length-first&&a[a.length-1-end]===b[b.length-1-end])end++;return [a.slice(first,a.length-end),b.slice(first,b.length-end)];}
const alias=(a,b)=>{const p=differingUnits(a,b);return aliases.some(x=>x.includes(p[0])&&x.includes(p[1]));};
function factor(a,b){const [x,y]=differingUnits(a,b);if(rules.groups.some(g=>g.includes(x)&&g.includes(y)))return rules.strictness;if(rules.atomic.includes(x)||rules.distinct.includes(x))return rules.leniency;return rules.baseline;}
function shape(font,text,cache){const b=new hb.Buffer();b.addText(text);b.guessSegmentProperties();hb.shape(font,b);const infos=b.getGlyphInfos(),pos=b.getGlyphPositions();if(infos.some(i=>i.codepoint===0))return null;const out=[];let x=0,y=0;for(let i=0;i<infos.length;i++){const gid=infos[i].codepoint;if(!cache.has(gid))cache.set(gid,parsePathData(font.glyphToPath(gid)));for(const c of cache.get(gid))out.push(c.map(([px,py])=>[(px+x+pos[i].xOffset)/font.upm,-(py+y+pos[i].yOffset)/font.upm]));x+=pos[i].xAdvance;y+=pos[i].yAdvance;}return glyphSignature(out);}
const hashes=new Set(),fonts=[];
for(const name of fs.readdirSync(F).filter(n=>/\.[ot]tf$/i.test(n)).sort()){const bytes=fs.readFileSync(`${F}/${name}`),hash=crypto.createHash('sha256').update(bytes).digest('hex');if(hashes.has(hash))continue;hashes.add(hash);const face=new hb.Face(new hb.Blob(bytes)),font=new hb.Font(face);font.upm=face.upem;font.setScale(face.upem,face.upem);fonts.push({name,font});}
const out=[];
for(const task of tasks){
 const file=`${OUT}/page-input/${task.name}-a4.svg`;fs.writeFileSync(file,`<svg xmlns="http://www.w3.org/2000/svg" width="595.276" height="841.89" viewBox="0 0 595.276 841.89">${task.query.map(p=>p.element).join('\n')}</svg>`);
 const query=glyphSignature(loadShapes(file).map(x=>x.contour)),votes=[],all=[];
 for(const {name,font} of fonts){const cache=new Map(),index=task.labels.map(text=>({text,signature:shape(font,text,cache)})).filter(x=>x.signature);if(index.length<2)continue;const r=matchContours(query,index,{minMargin:0});if(!r.matches?.length)continue;const best=r.matches[0],second=r.matches[1],ref=index.find(e=>e.text===best.text);let room=Infinity,rival=null;
  for(const e of index){if(e.text===best.text)continue;const dist=contourDistance(ref.signature,e.signature);if(dist<room){room=dist;rival=e.text;}}
  const need=Number.isFinite(room)?room*factor(best.text,rival):0,veto=second&&alias(best.text,second.text),accepted=r.accepted&&!veto&&r.margin>=need;
  const record={font:name,text:best.text,distance:best.distance,margin:Number.isFinite(r.margin)?r.margin:null,need,veto:Boolean(veto),accepted};all.push(record);if(accepted)votes.push(record);
 }
 const groups=new Map();for(const v of votes){const g=groups.get(v.text)||{text:v.text,votes:0,best:Infinity,fonts:[]};g.votes++;g.best=Math.min(g.best,v.distance);g.fonts.push(v.font);groups.set(v.text,g);}
 const scannerCandidates=task.labels.map(text=>{const repair=repairText(text,words),scan=scanText(repair.text,{dictionary:words,standalone:d.mapData?.newClause?.standalone||[]});return {text,dictionaryExact:words.has(text),repair,scan};});
 const result={task:task.name,sourceBytes:task.query.map(p=>p.code),sourceContours:query.count,sourceAspect:query.aspect,labels:task.labels,ranking:[...groups.values()].sort((a,b)=>b.votes-a.votes||a.best-b.best),closest:all.toSorted((a,b)=>a.distance-b.distance).slice(0,8),comparisons:all,scannerCandidates};out.push(result);console.log(JSON.stringify({task:result.task,sourceBytes:result.sourceBytes,sourceContours:result.sourceContours,ranking:result.ranking,closest:result.closest.slice(0,2)}));
}
fs.writeFileSync(`${OUT}/a4-cluster-results.json`,JSON.stringify({kind:'reconstructed targeted regression; NOT full extraction or a calibrated acceptance policy',a4Mapping:geom.mapping,referenceFiles:fonts.length,notes:'Known hypothesis set; exploratory contour-unit bars; no document layout selection; not original matcher accuracy',tasks:out},null,2)+'\n');
