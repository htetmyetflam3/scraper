# Pending changes to `master.json` (mapData.IMPOSTER)

Found while decoding `Praser/File/upload/pdf/mm-grammar.pdf` and currently
applied **on the Python side only** (`Praser/Python/modules/cleanup.py`).
They belong in the sealed map the next time it is regenerated, so the JS
engine and the praser stay identical.

## 1. `ဥ` cannot carry an asat

`ဉ` (U+1009) is the one that holds an asat, and the one that spells `ဉာဏ်`.
`ဥ` (U+1025) is for `ဥ`, `ဥုံ`, `ဥုံး`. The map already resolves this pair by
the FOLLOWING vowel in four places:

| correct | imposters | decided by |
| --- | --- | --- |
| `ဉာဋ်` | `ဥာဋ်` | ` ာ` |
| `ဦး` | `ဉီး`, `ဥိီး`, `ဉိီး` | ` ီး` |
| `ဥုံ` | `ဉုံ` | ` ု` |
| `ဥမ်` | `ဉမ်` | ` မ်` |

The **asat** context is missing. Evidence from the book's own text
(`scripts.db`, table `mmgrammar`): `ဥ်` occurs **0** times there and `ဉ်`
occurs **227**; this extraction produced **408** `ဥ်`.

```
ဥ်   ->  ဉ်
```

restores `မျဉ်း` (85), `ယှဉ်` (90), `အကျဉ်း` (18), `စီစဉ်` (10) and takes
`ဥ်` to 0. The `ဥ` that remain are all legitimate: `ဥ၊` in the alphabet
list, `ဣဤဥဦဧ`, `ပဥ္စမ`, `ရာသီဥတု`.

## 2. Write it TWICE — NFC reorders the pair

`U+1037` has combining class 7 and `U+103A` has 9, so NFC canonically
rewrites `103A 1037` as `1037 103A`. Normalisation runs BEFORE the imposter
table, so an entry written in engine order never matches a word that also
carries a dot below:

```
ဥ်    ->  ဉ်      as the engine writes it      (1025 103A)
ဥ့်   ->  ဉ့်           as NFC leaves it             (1025 1037 103A)
```

Without the second spelling, `စဉ့်ကွင်း`, `ညဉ့်` and `အဉ့်` are all missed.
**This applies to the whole IMPOSTER table, not just this entry**: any key
containing both an asat and a dot below needs the NFC spelling.
