/* FILE: wasm/box/c/shim.c — the box engine ABI on quickjs-ng.
 *
 * Exports (plain names, no glue):
 *   alloc(len) -> ptr            host writes bytes into linear memory
 *   outPtr()   -> ptr            result buffer of the last run()
 *   boot(loaderPtr, loaderLen, payloadPtr, payloadLen) -> status (0 = ok)
 *   run(mode, ptr, len) -> outLen   mode: 0=normalize 1=mapperNormalize 2=myNormalize
 *
 * Contract: the loader is obfuscated but NOT encrypted (bootstrap runs raw);
 * the payload is decrypt-TO-LOAD — boot runs the loader's bootModules against
 * the payload in-engine and keeps the three normalize functions. Payload
 * modules are obfuscated JavaScript source, not bytecode. UTF-8 in/out; hosts
 * pre-apply NFC. Modules are served from the decrypted pack by basename
 * ('./normalize.js', './sourceMap.js', './function.js', './sourceMapper.js';
 * './logen.js' is a stub).
 */
#include "quickjs.h"
#include <stdlib.h>
#include <string.h>

static JSRuntime *rt;
static JSContext *ctx;
static char *loader_src;
static size_t loader_len;
static const unsigned char *payload_ptr;
static size_t payload_len;
static JSValue fns[3];
static char *outbuf;
static size_t outcap;

static char *xstrndup(const char *s, size_t n) {
  char *p = malloc(n + 1);
  memcpy(p, s, n);
  p[n] = 0;
  return p;
}

static const char *base_name(const char *name) {
  const char *b = strrchr(name, '/');
  return b ? b + 1 : name;
}

static void drain(void) {
  JSContext *c;
  while (JS_ExecutePendingJob(rt, &c) > 0) {
  }
}

/* Stash the current JS exception as text in outbuf; the host prints it. */
static int fail(int code) {
  JSValue ex = JS_GetException(ctx);
  JSValue stack = JS_GetPropertyStr(ctx, ex, "stack");
  size_t n = 0, ns = 0;
  const char *s = JS_ToCStringLen(ctx, &n, ex);
  const char *ss = JS_IsUndefined(stack) ? NULL : JS_ToCStringLen(ctx, &ns, stack);
  size_t total = n + ns + 2;
  outbuf = realloc(outbuf, total);
  size_t k = 0;
  if (s) { memcpy(outbuf + k, s, n); k += n; }
  outbuf[k++] = '\n';
  if (ss) { memcpy(outbuf + k, ss, ns); k += ns; }
  outbuf[k] = 0;
  if (s) JS_FreeCString(ctx, s);
  if (ss) JS_FreeCString(ctx, ss);
  JS_FreeValue(ctx, stack);
  JS_FreeValue(ctx, ex);
  return code;
}

/* Serve every module request from the in-engine pack. */
static JSModuleDef *box_module_loader(JSContext *c, const char *module_name,
                                      void *opaque) {
  (void)opaque;
  const char *base = base_name(module_name);
  JSValue srcv;
  if (!strcmp(base, "loader.js")) {
    srcv = JS_NewStringLen(c, loader_src, loader_len);
  } else if (!strcmp(base, "logen.js")) {
    static const char stub[] = "export function debugLogInit() {}";
    srcv = JS_NewStringLen(c, stub, sizeof(stub) - 1);
  } else {
    JSValue pack = JS_GetPropertyStr(c, JS_GetGlobalObject(c), "__pack");
    srcv = JS_GetPropertyStr(c, pack, base);
    JS_FreeValue(c, pack);
  }
  if (JS_IsException(srcv) || JS_IsUndefined(srcv))
    return NULL;
  size_t slen;
  const char *s = JS_ToCStringLen(c, &slen, srcv);
  JSValue func = JS_Eval(c, s, slen, module_name,
                         JS_EVAL_TYPE_MODULE | JS_EVAL_FLAG_COMPILE_ONLY);
  JS_FreeCString(c, s);
  JS_FreeValue(c, srcv);
  if (JS_IsException(func))
    return NULL;
  /* v0.17 canonical: the module is already referenced once compiled. */
  JSModuleDef *m = (JSModuleDef *)JS_VALUE_GET_PTR(func);
  JS_FreeValue(ctx, func);
  return m;
}

__attribute__((export_name("alloc"))) void *box_alloc(int len) {
  if (len < 0)
    return NULL;
  char *p = malloc((size_t)len);
  return p;
}

__attribute__((export_name("outPtr"))) char *box_out_ptr(void) { return outbuf; }

__attribute__((export_name("boot"))) int box_boot(int lptr, int llen, int pptr,
                                                  int plen) {
  loader_src = xstrndup((const char *)(uintptr_t)lptr, (size_t)llen);
  loader_len = (size_t)llen;
  payload_ptr = (const unsigned char *)(uintptr_t)pptr;
  payload_len = (size_t)plen;

  rt = JS_NewRuntime();
  ctx = JS_NewContext(rt);
  JS_SetModuleLoaderFunc(rt, NULL, box_module_loader, NULL);

  /* entry 0: host-web-API polyfills the loader needs (TextDecoder runs at
   * decrypt-to-load; QuickJS core has no web APIs). */
  static const char entry0[] =
      "globalThis.TextDecoder = class { decode(a) {"
      " const u = a instanceof Uint8Array ? a : new Uint8Array(a);"
      " let s = '';"
      " for (let i = 0; i < u.length;) { const c = u[i++];"
      "  if (c < 128) s += String.fromCharCode(c);"
      "  else if (c < 224) s += String.fromCharCode(((c & 31) << 6) | (u[i++] & 63));"
      "  else if (c < 240) s += String.fromCharCode(((c & 15) << 12) | ((u[i++] & 63) << 6) | (u[i++] & 63));"
      "  else { let p = ((c & 7) << 18) | ((u[i++] & 63) << 12) | ((u[i++] & 63) << 6) | (u[i++] & 63);"
      "   p -= 65536; s += String.fromCharCode(55296 + (p >> 10), 56320 + (p & 1023)); } }"
      " return s; } };"
      "globalThis.TextEncoder = class { encode(s) {"
      " const out = [];"
      " for (const ch of s) { const cp = ch.codePointAt(0);"
      "  if (cp < 128) out.push(cp);"
      "  else if (cp < 2048) out.push(192 | (cp >> 6), 128 | (cp & 63));"
      "  else if (cp < 65536) out.push(224 | (cp >> 12), 128 | ((cp >> 6) & 63), 128 | (cp & 63));"
      "  else out.push(240 | (cp >> 18), 128 | ((cp >> 12) & 63), 128 | ((cp >> 6) & 63), 128 | (cp & 63)); }"
      " return new Uint8Array(out); } };";
  JSValue r = JS_Eval(ctx, entry0, sizeof(entry0) - 1, "polyfill.js",
                      JS_EVAL_TYPE_GLOBAL);
  if (JS_IsException(r))
    return 10;
  JS_FreeValue(ctx, r);

  /* entry 1: pull bootModules out of the loader module */
  static const char entry1[] =
      "import { bootModules } from './loader.js';\n"
      "globalThis.__bootModules = bootModules;\n";
  r = JS_Eval(ctx, entry1, sizeof(entry1) - 1, "boot-entry.js",
              JS_EVAL_TYPE_MODULE);
  drain();
  if (JS_IsException(r)) {
    JS_FreeValue(ctx, r);
    return fail(1);
  }
  JS_FreeValue(ctx, r);
  JSValue gprobe = JS_GetGlobalObject(ctx);
  JSValue fnprobe = JS_GetPropertyStr(ctx, gprobe, "__bootModules");
  int isfn = JS_IsFunction(ctx, fnprobe);
  JS_FreeValue(ctx, fnprobe);
  JS_FreeValue(ctx, gprobe);
  if (!isfn) {
    JS_ThrowReferenceError(ctx, "bootModules not installed (loader module eval failed)");
    return fail(9);
  }

  /* payload -> Uint8Array -> bootModules(payload) == the pack */
  JSValue ab = JS_NewArrayBufferCopy(ctx, payload_ptr, payload_len);
  static const char wrap_src[] = "(b)=>new Uint8Array(b)";
  JSValue wrap = JS_Eval(ctx, wrap_src, sizeof(wrap_src) - 1, "<wrap>",
                         JS_EVAL_TYPE_GLOBAL);
  JSValue args[1] = {ab};
  JSValue u8 = JS_Call(ctx, wrap, JS_UNDEFINED, 1, args);
  JS_FreeValue(ctx, wrap);
  JS_FreeValue(ctx, ab);
  JSValue glob = JS_GetGlobalObject(ctx);
  JSValue bootfn = JS_GetPropertyStr(ctx, glob, "__bootModules");
  JSValue pack = JS_Call(ctx, bootfn, JS_UNDEFINED, 1, (JSValue[]){u8});
  JS_FreeValue(ctx, bootfn);
  JS_FreeValue(ctx, u8);
  if (JS_IsException(pack))
    return fail(2);
  JS_SetPropertyStr(ctx, glob, "__pack", pack);

  /* entry 2: keep the three normalize functions */
  static const char entry2[] =
      "import { normalize, mapperNormalize, myNormalize } from './normalize.js';\n"
      "globalThis.__n = [normalize, mapperNormalize, myNormalize];\n";
  r = JS_Eval(ctx, entry2, sizeof(entry2) - 1, "norm-entry.js",
              JS_EVAL_TYPE_MODULE);
  drain();
  if (JS_IsException(r))
    return fail(3);
  JS_FreeValue(ctx, r);
  JSValue n = JS_GetPropertyStr(ctx, glob, "__n");
  for (int i = 0; i < 3; i++) {
    fns[i] = JS_GetPropertyUint32(ctx, n, (uint32_t)i);
    if (JS_IsException(fns[i]) || JS_IsUndefined(fns[i]))
      return fail(4);
  }
  JS_FreeValue(ctx, n);
  JS_FreeValue(ctx, glob);
  return 0;
}

__attribute__((export_name("run"))) int box_run(int mode, int ptr, int len) {
  if (mode < 0 || mode > 2 || !ctx)
    return -1;
  JSValue s = JS_NewStringLen(ctx, (const char *)(uintptr_t)ptr, (size_t)len);
  JSValue out = JS_Call(ctx, fns[mode], JS_UNDEFINED, 1, (JSValue[]){s});
  JS_FreeValue(ctx, s);
  if (JS_IsException(out))
    return fail(-2);
  size_t olen;
  const char *os = JS_ToCStringLen(ctx, &olen, out);
  if (outcap < olen + 1) {
    outbuf = realloc(outbuf, olen + 1);
    outcap = olen + 1;
  }
  memcpy(outbuf, os, olen);
  JS_FreeCString(ctx, os);
  JS_FreeValue(ctx, out);
  return (int)olen;
}

int main(void) { return 0; }
