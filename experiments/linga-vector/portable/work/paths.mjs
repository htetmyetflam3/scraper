import {fileURLToPath} from 'node:url';
export const HOME=fileURLToPath(new URL('../',import.meta.url));
export const OUT=fileURLToPath(new URL('./',import.meta.url)).replace(/\/$/,'');
export const ROOT=fileURLToPath(new URL('../snapshot/linga/',import.meta.url)).replace(/\/$/,'');
export const LEGACY=fileURLToPath(new URL('../assets/legacy-fonts/',import.meta.url)).replace(/\/$/,'');
export const PDF=fileURLToPath(new URL('../fixtures/bms.pdf',import.meta.url));
