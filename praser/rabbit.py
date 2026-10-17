"""Rabbit Zawgyi -> Unicode converter.

Rule table: the COMPLETE official Rabbit zg2uni table (118 rules), loaded
from the SHARED object dir <root>/map/zg2uni-rules.json -- the same file
Parser/parsed/text/rabbit.mjs reads, so the two ports cannot drift. Order is
significant and preserved. JS ``$N`` replacement refs are converted to
Python ``\\N`` at load (verified byte-identical output against the official
rabbit.js on real document text).

A single static method ``Rabbit.zg2uni`` applies the rules in order.
"""

import re

from paths import map_json


def _load_rules():
    return [
        {"from": r["from"], "to": re.sub(r"\$(\d)", r"\\\1", r["to"])}
        for r in map_json("zg2uni-rules")
    ]


class Rabbit:
    _RULES = _load_rules()

    # Precompiled once at import time: zg2uni runs per page on 7000+ page
    # documents, so the regex cache lookup per call is worth eliminating.
    _COMPILED = [(re.compile(r["from"]), r["to"]) for r in _RULES]

    @staticmethod
    def zg2uni(text):
        for rx, to in Rabbit._COMPILED:
            text = rx.sub(to, text)
        return text
