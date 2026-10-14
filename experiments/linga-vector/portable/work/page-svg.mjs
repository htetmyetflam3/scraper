// Reconstructed recovery exporter. Executes PDF.js with an SVG-only native
// surface and records transformed filled paths. Never rasterizes digital input.
import fs from 'node:fs';import {fileURLToPath} from 'node:url';
import {createCanvas,Path2D,DOMMatrix,ImageData,SvgExportFlag} from '@napi-rs/canvas';
Object.assign(globalThis,{Path2D,DOMMatrix,ImageData});
const {getDocument,OPS}=await import('pdfjs-dist/legacy/build/pdf.mjs');
import {loadShapes,inkContours,segmentLines} from '../snapshot/linga/Parsed/parser/shape/svg.mjs';
import {PDF,OUT} from './paths.mjs';
const assets=fileURLToPath(new URL('../../',import.meta.resolve('pdfjs-dist/legacy/build/pdf.mjs')));
let auxiliarySurfaces=0;
class NoAuxiliarySurfaces {create(){auxiliarySurfaces++;throw Error('Auxiliary surface disallowed');}reset(){throw Error('Auxiliary reset disallowed');}destroy(){}}
const task=getDocument({CanvasFactory:NoAuxiliarySurfaces,data:new Uint8Array(fs.readFileSync(process.argv[2]||PDF)),disableFontFace:true,fontExtraProperties:true,isEvalSupported:false,useSystemFonts:false,standardFontDataUrl:assets+'standard_fonts/',cMapUrl:assets+'cmaps/',cMapPacked:true,wasmUrl:assets+'wasm/'});
const doc=await task.promise;
fs.mkdirSync(`${OUT}/page-input`,{recursive:true});
const pages=process.argv[3]?[Number(process.argv[3])]:Array.from({length:doc.numPages},(_,i)=>i+1),summary=[];
for(const n of pages){
 const page=await doc.getPage(n),viewport=page.getViewport({scale:1}),ops=await page.getOperatorList();
 const labels=new Map();let fontId=null,textOps=0;
 for(let i=0;i<ops.fnArray.length;i++){
  if(ops.fnArray[i]===OPS.setFont)fontId=ops.argsArray[i][0];
  if(ops.fnArray[i]===OPS.showText){textOps++;const map=labels.get(fontId)||new Map();for(const g of ops.argsArray[i][0])if(g&&typeof g==='object')map.set(g.fontChar,g.originalCharCode);labels.set(fontId,map);}
 }
 let current=null,pixelOperations=0;auxiliarySurfaces=0;
 for(const [id,map] of labels){const font=page.commonObjs.get(id),original=font.getPathGenerator;font.getPathGenerator=function(objs,char){current={font:id,code:map.get(char)??null};return original.call(this,objs,char);};}
 const canvas=createCanvas(Math.ceil(viewport.width),Math.ceil(viewport.height),SvgExportFlag.NoPrettyXML),ctx=canvas.getContext('2d'),paths=[];
 const forbidden=new Set(['getImageData','putImageData','drawImage','createImageData','fillText','strokeText','createPattern']);
 const proxy=new Proxy(ctx,{get(target,key){
  if(forbidden.has(key))return()=>{pixelOperations++;throw Error(`Forbidden digital-lane operation: ${key}`);};
  if(key==='fill')return(path,rule)=>{if(!(path instanceof Path2D))throw Error('Untracked current-path fill');const transformed=new Path2D();transformed.addPath(path,target.getTransform());const d=transformed.toSVGString();if(d)paths.push({...current,d});};
  if(key==='stroke')return()=>{throw Error('Stroke export is not implemented; do not silently discard it');};
  const v=Reflect.get(target,key,target);return typeof v==='function'?v.bind(target):v;
 },set(target,key,value){return Reflect.set(target,key,value,target);}});
 const factory={create(){auxiliarySurfaces++;throw Error('Auxiliary surface disallowed');},reset(){throw Error('Auxiliary surface reset disallowed');},destroy(){}};
 await page.render({canvas:null,canvasContext:proxy,viewport,canvasFactory:factory,background:'rgba(255,255,255,1)'}).promise;
 if(textOps&&!paths.length)throw Error('Text page produced zero captured paths');
 const svg=`${OUT}/page-input/bms-page${n}.svg`;
 const escape=s=>String(s??'').replaceAll('&','&amp;').replaceAll('"','&quot;');
 fs.writeFileSync(svg,`<svg xmlns="http://www.w3.org/2000/svg" width="${viewport.width}" height="${viewport.height}" viewBox="0 0 ${viewport.width} ${viewport.height}">\n${paths.map(p=>`<path data-font="${escape(p.font)}" data-source-code="${escape(p.code)}" d="${p.d}"/>`).join('\n')}\n</svg>\n`);
 const shapes=loadShapes(svg),ink=inkContours(shapes),lines=segmentLines(ink);
 const report={page:n,width:viewport.width,height:viewport.height,paths:paths.length,contours:shapes.length,inkContours:ink.length,lineGroups:lines.length,pixelOperations,auxiliarySurfaces,textOps,exporter:'reconstructed recovery exporter; SVG surface, canvas:null, baked transforms'};
 fs.writeFileSync(`${OUT}/page-input/bms-page${n}-geometry.json`,JSON.stringify(report,null,2)+'\n');summary.push(report);console.log(JSON.stringify(report));
}
await task.destroy();fs.writeFileSync(`${OUT}/page-input/summary.json`,JSON.stringify(summary,null,2)+'\n');
