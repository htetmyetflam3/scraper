// eslint-disable-next-line no-unused-vars -- intentional unused variable in test/experimental code
import { MMap, NewMap, newClause, PUNCTUATION } from "./sourceMap.js";
import {
	compose,
	decompose,
	burmeseTokenToAscii,
	asciiTokenToBurmese,
} from "./function.js";
const _mmToAscii = new Map();
const _asciiToMm = new Map();
function _pair(mmArr, ascArr) {
	mmArr.forEach((mm, i) => {
		_mmToAscii.set(mm, ascArr[i]);
		_asciiToMm.set(ascArr[i], mm);
	});
}
_pair(MMap.Base, NewMap.Base);
_pair(MMap.Tail, NewMap.Tail);
_pair(MMap.Asat, NewMap.Asat);
_pair(MMap.Num, NewMap.Num);
function* iterate(str) {
	let i = 0;
	while (i < str.length) {
		const ch = str[i];
		if (ch === "{") {
			const end = str.indexOf("}", i + 1);
			if (end !== -1) {
				yield { type: "bypass", content: str.slice(i, end + 1) };
				i = end + 1;
				continue;
			}
		}
		yield { type: "token", char: ch, index: i };
		i++;
	}
}
export function mapperStr(str) {
	let out = "";
	for (const item of iterate(str)) {
		if (item.type === "bypass") {
			out += item.content;
		} else {
			out += burmeseTokenToAscii(item.char, _mmToAscii) ?? item.char;
		}
	}
	out = compose(out, _asciiToMm);
	return out;
}
export function toBurmeseStr(str) {
	const decomposed = decompose(str, _mmToAscii, _asciiToMm);
	let mm = "";
	for (const item of iterate(decomposed)) {
		if (item.type === "bypass") {
			mm += item.content;
		} else {
			mm += asciiTokenToBurmese(item.char, _asciiToMm) ?? item.char;
		}
	}
	return mm.replace(/[{}]/g, "");
}
const _tailLookup = {};
for (const tok of NewMap.Tail) {
	_tailLookup[tok] = "Mark";
}
_tailLookup[NewMap.Asat[0]] = "asat";
for (let i = 0; i < 26; i++) {
	_tailLookup[String.fromCharCode(97 + i)] = "baseasat";
}
export function isTail(tok) {
	return _tailLookup[tok] ?? false;
}
export function isBase(tok) {
	return NewMap.Base.includes(tok);
}
export function isNum(tok) {
	return NewMap.Num.includes(tok);
}
export function isStick(tok) {
	return newClause.stick.includes(tok);
}
export function isStandalone(tok) {
	return newClause.standalone.includes(tok);
}
