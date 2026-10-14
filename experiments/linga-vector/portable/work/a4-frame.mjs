import fs from 'node:fs';import {Path2D,DOMMatrix} from '@napi-rs/canvas';
import {loadShapes,inkContours,segmentLines} from '../snapshot/linga/Parsed/parser/shape/svg.mjs';
import {OUT} from './paths.mjs';
export const A4={width:595.276,height:841.89};
export function frame(width,height){if(!(width>0&&height>0))throw Error('Invalid page dimensions');const scale=Math.min(A4.width/width,A4.height/height);return {scale,tx:(A4.width-width*scale)/2,ty:(A4.height-height*scale)/2,width:A4.width,height:A4.height};}
for(let page=1;page<=9;page++){
 const prefix=`${OUT}/page-input/bms-page${page}`,svg=fs.readFileSync(`${prefix}.svg`,'utf8');
 if(/<image\b|<text\b|\btransform=/.test(svg))throw Error('Input is not baked paths only');
 const width=Number(svg.match(/\bwidth="([^"]+)"/)[1]),height=Number(svg.match(/\bheight="([^"]+)"/)[1]),f=frame(width,height),matrix=new DOMMatrix([f.scale,0,0,f.scale,f.tx,f.ty]);
 const paths=[...svg.matchAll(/<path\b[^>]*\bd="([^"]*)"[^>]*\/>/g)].map(m=>{const p=new Path2D();p.addPath(new Path2D(m[1]),matrix);return m[0].replace(/\bd="[^"]*"/,`d="${p.toSVGString()}"`);});
 const file=`${prefix}-a4.svg`;fs.writeFileSync(file,`<svg xmlns="http://www.w3.org/2000/svg" width="${A4.width}" height="${A4.height}" viewBox="0 0 ${A4.width} ${A4.height}" data-uniform-scale="${f.scale}">\n${paths.join('\n')}\n</svg>\n`);
 const shapes=loadShapes(file),ink=inkContours(shapes),lines=segmentLines(ink),report={page,source:`bms-page${page}.svg`,svg:`bms-page${page}-a4.svg`,sourceSize:{width,height},a4:A4,mapping:f,rasterized:false,paths:paths.length,contours:shapes.length,inkContours:ink.length,lineGroups:lines.length};
 fs.writeFileSync(`${prefix}-a4-geometry.json`,JSON.stringify(report,null,2)+'\n');console.log(JSON.stringify(report));
}
