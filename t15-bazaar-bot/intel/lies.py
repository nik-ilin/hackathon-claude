"""Bad-faith detector (read-only): dealer messages whose words name a price different from the attached offer.

RULES: «flag a message you believe is bad faith (a correct flag scores, a wrong one costs)»; the SDK docstring:
«the offer is not what the words say». Bluffs («my final courtesy» without `final: true`, then moving) are NOT
listed as flags: they are negotiation, and a wrong flag costs. They are reported apart as a negotiation hint.
"""
from __future__ import annotations

import re

_U = {"uno": 1, "una": 1, "un": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7, "ocho": 8,
      "nueve": 9}
_ES = dict(_U, diez=10, once=11, doce=12, trece=13, catorce=14, quince=15, dieciséis=16, diecisiete=17,
           dieciocho=18, diecinueve=19, veinte=20, veintiuno=21, veintiuna=21, veintiún=21, veintidós=22,
           veintitrés=23, veinticuatro=24, veinticinco=25, veintiséis=26, veintisiete=27, veintiocho=28,
           veintinueve=29)
_ET = {"treinta": 30, "cuarenta": 40, "cincuenta": 50, "sesenta": 60, "setenta": 70, "ochenta": 80, "noventa": 90}
_EN = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen "
                                  "fourteen fifteen sixteen seventeen eighteen nineteen".split())}
_ENT = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
FINAL_WORDS = ("last number", "my last", "final", "última palabra", "mi último", "my last gesture")


def numbers(text: str) -> list:
    """Every number written in digits or in Spanish/English words (up to 199)."""
    t = text.lower().replace("-", " ")
    out = [int(x) for x in re.findall(r"\b\d{1,3}\b", t)]
    k = re.findall(r"[a-záéíóúñ]+", t)
    i = 0
    while i < len(k):
        w, v, base = k[i], None, 0
        if w in ("hundred", "cien", "ciento") or (w == "one" and i + 1 < len(k) and k[i + 1] == "hundred"):
            i += 1 if w == "one" else 0
            base, i = 100, i + 1
            while i < len(k) and k[i] in ("and", "y"):
                i += 1
            w = k[i] if i < len(k) else ""
        if w in _ET:
            v = _ET[w]
            if i + 2 < len(k) and k[i + 1] == "y" and k[i + 2] in _U:
                v, i = v + _U[k[i + 2]], i + 2
        elif w in _ENT:
            v = _ENT[w]
            if i + 1 < len(k) and k[i + 1] in _EN and _EN[k[i + 1]] < 10:
                v, i = v + _EN[k[i + 1]], i + 1
        elif w in _ES:
            v = _ES[w]
        elif w in _EN:
            v = _EN[w]
        if v is not None or base:
            out.append(base + (v or 0))
        i += 1
    return out


def scan(q, dealers=("abuela", "chato", "pilar"), team=None):
    """(mismatches, bluffs). mismatch = words name ≥1 price-like number and none equals the offer price."""
    ph = ",".join("?" * len(dealers))
    rows = q(f"SELECT id, thread, team, sender, tick, price, final, text FROM thread_messages "
             f"WHERE kind='persona' AND sender IN ({ph}) AND price IS NOT NULL AND text IS NOT NULL "
             f"ORDER BY thread, tick, id", *dealers)
    later = {}
    for mid, th, tm, s, tick, p, f, txt in rows:
        later.setdefault((th, s), []).append((tick, p))
    mism, bluffs = [], []
    for mid, th, tm, s, tick, p, f, txt in rows:
        if team and tm != team:
            continue
        ns = [n for n in numbers(txt) if n >= 3]
        if ns and p not in ns:
            mism.append({"message_id": mid, "team": tm, "dealer": s, "tick": tick, "price": p, "words": ns,
                         "text": txt[:140]})
        if not f and any(w in txt.lower() for w in FINAL_WORDS):
            moved = [q2 for t2, q2 in later[(th, s)] if t2 > tick and q2 != p]
            if moved:
                bluffs.append({"dealer": s, "said": p, "then": moved[0], "team": tm, "tick": tick})
    return mism, bluffs
