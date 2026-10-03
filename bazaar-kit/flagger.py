"""MENSAJES DE MALA FE — detector y flags (opt-in; dry run por defecto).

RULES.md: «Some lie; flag a message you believe is bad faith with POST /api/flags (a correct flag scores, a wrong one
costs)». El SDK concreta: «the offer is not what the words say». Este módulo compara las PALABRAS de cada mensaje de
un dealer en nuestros hilos con la OFERTA estructurada que lleva adjunta y solo propone flag cuando la discrepancia es
clara (confianza alta):

  precio    las palabras dan ≥1 precio con marca de moneda («17 P», «dieciséis pesetas», «for twenty»), NINGÚN número
            del texto coincide con el precio de la oferta y la diferencia mínima es ≥ max(3 P, 10 %).
  carta     el texto nombra cartas del catálogo (nombre o «LAV-03») y ninguna es la de la oferta, que sí lleva carta.
  sobre     el texto nombra un tipo de sobre («Gold pack», «sobre de plata») distinto del que da la oferta.
  dirección las palabras prometen PAGARNOS («I pay you 20 P», «te pago») y la oferta nos COBRA (want.cash, sin give.cash).
  cantidad  «three cards», «dos sobres»... y la oferta da otro número de artículos.

Nunca son flag: los faroles («final», «my last number» sin `final: true`) — son negociación, no mentira —, los números
sin marca de moneda que no coinciden (años, movimientos, «Bajaste cinco»: quedan como `weak`, solo informativos) y los
mensajes sin oferta adjunta. Nunca se marca dos veces el mismo mensaje (estado en fichero local).

    python3 flagger.py                       # dry run: lista candidatos en nuestros hilos con dealers
    python3 flagger.py --execute             # sdk.flag() de los candidatos fuertes no marcados antes
    python3 flagger.py --validate-db PATH    # recorre thread_messages de un market.db (solo lectura) y cuenta

Para el negociador: `offer_matches_words(text, offer, catalog)` devuelve False si las palabras no casan con la oferta;
`safe_to_accept(thread, offer_id, catalog)` lo aplica al mensaje que trae esa oferta. Opt-in: coordinator y
ladder_plus pueden llamarlo antes de `accept()`; no cambia su comportamiento por defecto.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Iterable, Optional

DEFAULT_URL = os.environ.get("BAZAAR_URL", "https://bazaar.causaprima.ai")
DEFAULT_STATE = os.environ.get("FLAGGER_STATE", "flagger_state.json")
DEALERS_DEFAULT = ("abuela", "chato", "pilar", "picaros")
TEAM_RE = re.compile(r"^t\d{2}$")

# ------------------------------------------------------------------ números en palabras (es/en) y dígitos

_U = {"uno": 1, "una": 1, "un": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7, "ocho": 8,
      "nueve": 9}
_ES = dict(_U, cero=0, diez=10, once=11, doce=12, trece=13, catorce=14, quince=15, dieciséis=16, dieciseis=16,
           diecisiete=17, dieciocho=18, diecinueve=19, veinte=20, veintiuno=21, veintiuna=21, veintiún=21,
           veintidós=22, veintidos=22, veintitrés=23, veintitres=23, veinticuatro=24, veinticinco=25, veintiséis=26,
           veintiseis=26, veintisiete=27, veintiocho=28, veintinueve=29)
_ET = {"treinta": 30, "cuarenta": 40, "cincuenta": 50, "sesenta": 60, "setenta": 70, "ochenta": 80, "noventa": 90}
_EN = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen "
                                  "fourteen fifteen sixteen seventeen eighteen nineteen".split())}
_ENT = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_HUNDREDS = {"doscientos": 200, "doscientas": 200, "trescientos": 300, "trescientas": 300, "cuatrocientos": 400,
             "cuatrocientas": 400, "quinientos": 500, "quinientas": 500}

CURRENCY = {"p", "primas", "prima", "pesetas", "peseta", "€", "euros", "euro", "coins", "monedas", "duros", "pts"}
PRICE_BEFORE = {"for", "por", "at"}
# unidades que convierten un número en algo que NO es un precio («forty years», «three moves»)
NON_PRICE_UNITS = {"years", "year", "años", "año", "moves", "move", "minutes", "minutos", "hours", "horas", "days",
                   "días", "dias", "ticks", "tick", "times", "veces", "kilos", "children", "niños", "steps", "pasos",
                   "percent", "%", "sundays", "domingos", "generations", "printed", "copies", "copias", "cards",
                   "cartas", "packs", "sobres", "cromos"}
FINAL_WORDS = ("last number", "my last", "final", "última palabra", "mi último", "my last gesture")
_REF_RE = re.compile(r"\b[A-Z]{3}-\d{2}\b")


@dataclass
class Num:
    value: int
    start: int            # índice de token
    end: int              # índice del último token
    tagged: bool = False  # lleva marca de moneda o «por/for»
    unit: Optional[str] = None


def _tokens(text: str) -> list:
    t = _REF_RE.sub(" ", text or "")
    t = re.sub(r"#?\d+\s*/\s*\d+", " ", t)          # números de serie («#7/30»)
    t = t.lower().replace("-", " ")
    return re.findall(r"\d+|[a-záéíóúüñ€%]+|[.!?;:]", t)   # la puntuación corta números y unidades


def parse_numbers(text: str) -> list:
    """Números en dígitos o en palabras (es/en, compuestos hasta 599) con su posición y si llevan marca de precio."""
    k = _tokens(text)
    out = []
    i = 0
    while i < len(k):
        start, w, v, base = i, k[i], None, 0
        if w.isdigit():
            v = int(w) if len(w) <= 4 else None
        else:
            if w in _HUNDREDS or w in ("hundred", "cien", "ciento") or (w in ("one", "a") and i + 1 < len(k)
                                                                      and k[i + 1] == "hundred"):
                if w in ("one", "a"):
                    i += 1
                base = _HUNDREDS.get(k[i], 100)
                i += 1
                while i < len(k) and k[i] in ("and", "y"):
                    i += 1
                w = k[i] if i < len(k) else ""
            if w in _ET:
                v = _ET[w]
                if i + 2 < len(k) and k[i + 1] == "y" and k[i + 2] in _U:
                    v, i = v + _U[k[i + 2]], i + 2
            elif w in _ENT:
                v = _ENT[w]
                if i + 1 < len(k) and k[i + 1] in _EN and 0 < _EN[k[i + 1]] < 10:
                    v, i = v + _EN[k[i + 1]], i + 1
            elif w in _ES:
                v = _ES[w]
            elif w in _EN:
                v = _EN[w]
            elif base:
                i -= 1              # «cien» solo: la palabra siguiente no era parte del número
        if v is not None or base:
            n = Num(base + (v or 0), start, i)
            nxt = k[i + 1] if i + 1 < len(k) else ""
            prv = k[start - 1] if start > 0 else ""
            n.unit = nxt if nxt in NON_PRICE_UNITS else None
            n.tagged = n.unit is None and (nxt in CURRENCY or prv in PRICE_BEFORE)
            out.append(n)
        i += 1
    return out


def numbers(text: str) -> list:
    """Compatibilidad con intel/lies.py: solo los valores."""
    return [n.value for n in parse_numbers(text)]


# ------------------------------------------------------------------ catálogo: cartas y sobres

PACK_WORDS = {
    "sobre_barrio": ("neighbourhood pack", "neighborhood pack", "sobre de barrio", "sobre_barrio", "sobre del barrio"),
    "sobre_plata": ("silver pack", "sobre de plata", "sobre_plata"),
    "sobre_oro": ("gold pack", "golden pack", "sobre de oro", "sobre_oro"),
}


def card_names(catalog: Optional[dict]) -> dict:
    """nombre en minúsculas -> ref (solo nombres de ≥2 palabras o ≥8 letras, para no confundir palabras sueltas)."""
    out = {}
    for s in (catalog or {}).get("sets", []):
        for c in s.get("cards", []):
            nm = (c.get("name") or "").strip().lower()
            if nm and (" " in nm or len(nm) >= 8):
                out[nm] = c["id"]
    return out


def cards_named(text: str, catalog: Optional[dict]) -> set:
    refs = set(_REF_RE.findall(text or ""))
    low = (text or "").lower()
    for nm, ref in card_names(catalog).items():
        if re.search(r"(?<![\wáéíóúñ])" + re.escape(nm) + r"(?![\wáéíóúñ])", low):
            refs.add(ref)
    return refs


def packs_named(text: str) -> set:
    low = (text or "").lower()
    return {p for p, words in PACK_WORDS.items() if any(w in low for w in words)}


def _refs_of_side(side: dict) -> set:
    out = set()
    for a in side.get("assets") or []:
        if isinstance(a, dict) and a.get("kind", "card") == "card" and a.get("ref"):
            out.add(a["ref"])
    for t in list(side.get("types") or []) + ["card:" + c for c in side.get("cards") or []]:
        if isinstance(t, str) and t.startswith("card:"):
            out.add(t[5:])
    return out


def _packs_of_side(side: dict) -> set:
    out = {t[5:] for t in side.get("types") or [] if isinstance(t, str) and t.startswith("pack:")}
    out |= {a.get("ref") for a in side.get("assets") or [] if isinstance(a, dict) and a.get("kind") == "pack"}
    return out


def _items_of_side(side: dict) -> int:
    return len(side.get("assets") or []) + len(side.get("types") or []) + len(side.get("cards") or [])


def offer_price(offer: dict) -> Optional[int]:
    """Precio en primas de una oferta de dealer: lo que pide (want.cash) o, si compra, lo que paga (give.cash)."""
    w, g = (offer.get("want") or {}).get("cash") or 0, (offer.get("give") or {}).get("cash") or 0
    return int(w) if w else (int(g) if g else None)


# ------------------------------------------------------------------ comparación palabras ↔ oferta

ASIDE_WORDS = ("present", "regal", "gift", "take this", "take el", "take la", "también", "too,", "toma ", "pregunt",
               "ask her", "ask him", "ask about", "dorada", "golden", "legend", "leyenda")
QUANTITY_UNITS ={"cards", "cartas", "packs", "sobres", "cromos", "copies", "copias"}
PAY_US = re.compile(r"\b(i(?:'ll| will)? pay you|i pay you|te pago|te pagaré|le pago|paid to you|i give you \d+|"
                    r"i(?:'ll| will)? give you (?:\d+|[a-z]+) (?:p|primas|pesetas))\b")


@dataclass
class Finding:
    kind: str                 # price | card | pack | direction | quantity
    strong: bool              # True = candidato a flag; False = solo informativo
    detail: str
    words: list = field(default_factory=list)


def tolerance(price: int) -> int:
    return max(3, int(math.ceil(0.10 * price)))


def compare(text: str, offer: Optional[dict], catalog: Optional[dict] = None, topic: Optional[dict] = None) -> list:
    """Discrepancias entre las palabras y la oferta adjunta (vacío = casan, o no hay oferta que comparar)."""
    if not offer or not text:
        return []
    out = []
    give, want = offer.get("give") or {}, offer.get("want") or {}
    nums = parse_numbers(text)
    price = offer_price(offer)
    if price is not None:
        vals = {n.value for n in nums}
        if price not in vals:
            tagged = [n.value for n in nums if n.tagged and n.value >= 1]
            loose = [n.value for n in nums if not n.tagged and n.unit is None and n.value >= 3]
            tol = tolerance(price)
            if tagged and min(abs(v - price) for v in tagged) >= tol:
                out.append(Finding("price", True, f"las palabras dicen {tagged} y la oferta {price} P", tagged))
            elif tagged or loose:
                out.append(Finding("price", False, f"números {tagged or loose} ≠ {price} P (sin confianza)",
                                   tagged or loose))
    low = (text or "").lower()
    # carta: la oferta lleva carta(s) concretas y el texto nombra otras. Abuela regala de verdad cartas «de propina»
    # (gift.given) y Pilar/Abuela cuentan la leyenda de la chulapa dorada: con esas marcas, o si nombra varias, es débil.
    offer_refs = _refs_of_side(give) | _refs_of_side(want)
    named = cards_named(text, catalog)
    if offer_refs and named and not (named & offer_refs):
        aside = len(named) > 1 or any(w in low for w in ASIDE_WORDS)
        out.append(Finding("card", not aside, f"el texto nombra {sorted(named)} y la oferta lleva "
                                              f"{sorted(offer_refs)}" + (" (aparte/regalo)" if aside else ""),
                           sorted(named)))
    # sobre: el texto nombra un tipo de sobre y la oferta da OTRO sobre. Si la oferta da una carta, es débil: Chato
    # describe sus cartas como «silver pack, good one».
    offer_packs = _packs_of_side(give)
    said_packs = packs_named(text)
    if said_packs and (offer_packs or _refs_of_side(give)) and not (said_packs & offer_packs):
        out.append(Finding("pack", bool(offer_packs), f"el texto habla de {sorted(said_packs)} y la oferta da "
                                                      f"{sorted(offer_packs) or sorted(_refs_of_side(give))}",
                           sorted(said_packs)))
    # dirección: promete pagarnos y la oferta nos cobra
    if PAY_US.search(low) and (want.get("cash") or 0) > 0 and not (give.get("cash") or 0):
        out.append(Finding("direction", True, f"promete pagarnos y la oferta nos cobra {want.get('cash')} P"))
    # cantidad: «three cards» con una oferta de otro número de artículos (el lado que nos da)
    n_items = _items_of_side(give) or _items_of_side(want)
    toks = _tokens(text)
    for n in nums:
        nxt = toks[n.end + 1] if n.end + 1 < len(toks) else ""
        if nxt in QUANTITY_UNITS and n.value >= 2 and n_items and n.value != n_items:
            out.append(Finding("quantity", True, f"«{n.value} {nxt}» y la oferta mueve {n_items} artículo(s)",
                               [n.value]))
            break
    return out


def offer_matches_words(text: str, offer: Optional[dict], catalog: Optional[dict] = None,
                        topic: Optional[dict] = None) -> bool:
    """False si las palabras contradicen claramente la oferta (mismo criterio que un flag). Faroles: True."""
    return not any(f.strong for f in compare(text, offer, catalog, topic))


def is_bluff(text: str, offer: Optional[dict]) -> bool:
    """«final»/«my last number» sin `final: true`: negociación, NUNCA flag."""
    return bool(offer) and not offer.get("final") and any(w in (text or "").lower() for w in FINAL_WORDS)


def safe_to_accept(thread: dict, offer_id, catalog: Optional[dict] = None) -> tuple:
    """(ok, motivo) para aceptar `offer_id` de un hilo: busca el mensaje que la trae y compara palabras y estructura."""
    for m in thread.get("messages") or []:
        o = m.get("offer") or {}
        if o.get("id") == offer_id:
            bad = [f for f in compare(m.get("text") or "", o, catalog, thread.get("topic")) if f.strong]
            return (not bad, "; ".join(f.detail for f in bad) or "palabras y oferta casan")
    return True, "sin mensaje asociado a la oferta"


# ------------------------------------------------------------------ candidatos en hilos

def candidates(threads: Iterable, team: str, catalog: Optional[dict] = None, already: Iterable = ()) -> list:
    """Mensajes de dealers en nuestros hilos cuya oferta contradice las palabras (strong), sin repetir ya marcados."""
    done = {int(x) for x in already}
    out = []
    for th in threads:
        other = th.get("with")
        if other == team or (other and TEAM_RE.match(str(other))):
            continue                                  # solo hilos con dealers
        for m in th.get("messages") or []:
            s = m.get("sender")
            if not s or s == team or TEAM_RE.match(str(s)) or m.get("id") in done:
                continue
            o = m.get("offer")
            if o and o.get("to") not in (None, team):
                continue
            fs = compare(m.get("text") or "", o, catalog, th.get("topic"))
            strong = [f for f in fs if f.strong]
            if strong:
                out.append({"message_id": m.get("id"), "thread": th.get("id"), "dealer": s, "tick": m.get("tick"),
                            "price": offer_price(o or {}), "findings": [f.__dict__ for f in strong],
                            "bluff": is_bluff(m.get("text") or "", o),
                            "reason": "; ".join(f.detail for f in strong)[:280],
                            "text": (m.get("text") or "")[:200]})
    return out


# ------------------------------------------------------------------ estado local (nunca dos flags al mismo mensaje)

def load_state(path: str) -> dict:
    try:
        with open(path) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path: str, state: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)


def flagged_ids(state: dict) -> set:
    return {int(k) for k in (state.get("flagged") or {})}


def run_flags(api, cands: list, state: dict, state_path: str, execute: bool) -> list:
    """Marca los candidatos (con --execute) y apunta cada id ANTES de enviar: un fallo nunca provoca un segundo flag."""
    sent = []
    for c in cands:
        mid = c["message_id"]
        if mid is None or int(mid) in flagged_ids(state):
            continue
        if not execute:
            print(f"[dry] flag #{mid} ({c['dealer']}, hilo {c['thread']}): {c['reason']}")
            continue
        state.setdefault("flagged", {})[str(mid)] = {"reason": c["reason"], "status": "sending"}
        save_state(state_path, state)
        try:
            resp = api.flag(int(mid), c["reason"])
            state["flagged"][str(mid)].update(status="sent", response=resp)
        except Exception as e:  # noqa: BLE001 - se registra y no se reintenta
            state["flagged"][str(mid)].update(status="error", error=str(e)[:200])
        save_state(state_path, state)
        sent.append(mid)
        print(f"flag #{mid}: {state['flagged'][str(mid)]['status']}")
    return sent


def fetch_threads(api) -> list:
    raw = api.my_threads()
    lst = raw.get("threads", raw) if isinstance(raw, dict) else raw
    out = []
    for t in lst or []:
        if TEAM_RE.match(str(t.get("with") or "")):
            continue
        full = t if t.get("messages") is not None else api.thread(t["id"])
        out.append(full.get("thread", full) if isinstance(full, dict) else full)
    return out


# ------------------------------------------------------------------ validación contra market.db (solo lectura)

def validate_db(path: str, catalog: Optional[dict], dealers=("abuela", "chato", "pilar"), team: Optional[str] = None):
    import sqlite3
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    ph = ",".join("?" * len(dealers))
    q = (f"SELECT m.id, m.thread, m.team, m.sender, m.tick, m.text, o.payload FROM thread_messages m "
         f"LEFT JOIN offers o ON o.id = m.offer_id WHERE m.sender IN ({ph}) AND m.text IS NOT NULL")
    args = list(dealers)
    if team:
        q += " AND m.team = ?"
        args.append(team)
    total = with_offer = 0
    strong, weak, bluffs = [], [], 0
    for mid, th, tm, s, tick, text, payload in con.execute(q, args):
        total += 1
        o = json.loads(payload) if payload else None
        if not o:
            continue
        with_offer += 1
        bluffs += is_bluff(text, o)
        for f in compare(text, o, catalog):
            (strong if f.strong else weak).append({"message_id": mid, "team": tm, "dealer": s, "kind": f.kind,
                                                   "detail": f.detail, "text": text[:160]})
    return {"messages": total, "with_offer": with_offer, "strong": strong, "weak": weak, "bluffs": bluffs}


# ------------------------------------------------------------------ CLI

def _load_catalog(api, path: Optional[str]) -> Optional[dict]:
    if path:
        with open(path) as fh:
            return json.load(fh)
    return api.catalog() if api else None


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--execute", action="store_true", help="envía los flags (por defecto, dry run)")
    ap.add_argument("--state", default=DEFAULT_STATE, help="fichero local con los mensajes ya marcados")
    ap.add_argument("--catalog", help="catálogo JSON local (si no, GET /api/catalog)")
    ap.add_argument("--validate-db", metavar="MARKET_DB", help="recorre thread_messages de un market.db y sale")
    ap.add_argument("--team", help="con --validate-db: solo los mensajes a este equipo")
    ap.add_argument("--show", type=int, default=10, help="ejemplos a imprimir con --validate-db")
    a = ap.parse_args(argv)

    if a.validate_db:
        cat = None
        if a.catalog:
            cat = _load_catalog(None, a.catalog)
        r = validate_db(a.validate_db, cat, team=a.team)
        print(f"mensajes de dealers: {r['messages']} · con oferta: {r['with_offer']} · faroles: {r['bluffs']} · "
              f"candidatos fuertes (flag): {len(r['strong'])} · débiles (no flag): {len(r['weak'])}")
        for x in r["strong"][:a.show]:
            print("  FUERTE", x)
        for x in r["weak"][:a.show]:
            print("  débil ", x)
        return 0

    from bazaar_sdk import Bazaar
    key = os.environ.get("BAZAAR_KEY")
    if not key:
        print("falta BAZAAR_KEY en el entorno", file=sys.stderr)
        return 2
    api = Bazaar(DEFAULT_URL, key)
    team = api.me().get("id")
    cat = _load_catalog(api, a.catalog)
    state = load_state(a.state)
    cands = candidates(fetch_threads(api), team, cat, flagged_ids(state))
    print(f"{len(cands)} candidato(s) a flag en hilos de {team} con dealers ({'EXECUTE' if a.execute else 'dry run'})")
    run_flags(api, cands, state, a.state, a.execute)
    return 0


if __name__ == "__main__":
    sys.exit(main())
