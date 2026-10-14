// Query actual A4 page paths. Report raw bytes, NOT Unicode labels.
import fs from 'node:fs';import {parsePathData} from '../snapshot/linga/Parsed/parser/shape/svg.mjs';
import {glyphSignature} from '../snapshot/linga/Parsed/parser/shape/contour.mjs';
import {crowdVote} from '../snapshot/linga/Parsed/parser/codepage/outline-vote.mjs';import {OUT} from './paths.mjs';
const page=fs.readFileSync(`${OUT}/page-input/world-word-a4.svg`,'utf8'),r=JSON.parse(fs.readFileSync(`${OUT}/corrected-witnesses.json`,'utf8'));
function unpack(v){const b=Buffer.from(v.p,'base64'),u=new Uint16Array(b.buffer.slice(b.byteOffset,b.byteOffset+b.byteLength)),points=[];let j=0;for(let c=0;c<v.c;c++){const a=[];for(let i=0;i<r.samples;i++)a.push([u[j++]/1000,u[j++]/1000]);points.push(a);}return {count:v.c,aspect:v.r/1000,areas:v.a.map(x=>x/1000),points};}
const faces=r.faces.map(f=>({name:f.name,byByte:new Map(Object.entries(f.bytes).map(([b,v])=>[Number(b),unpack(v)]))})),results=[];
for(const m of page.matchAll(/<path data-font="([^"]*)" data-source-code="([^"]*)"[^>]*d="([^"]*)"\/>/g)){
 const sig=glyphSignature(parsePathData(m[3]).map(c=>c.map(([x,y])=>[x,-y])),{samples:r.samples});const vote=crowdVote(sig,faces),record={sourceByte:Number(m[2]),contours:sig.count,vote};results.push(record);console.log(JSON.stringify(record));
}
fs.writeFileSync(`${OUT}/a4-page-witness-results.json`,JSON.stringify({query:'reconstructed actual A4 page SVG',yConvention:'whole-query y reflection only to existing y-up witness convention',unicodeAuthority:false,witnesses:faces.length,results},null,2)+'\n');
