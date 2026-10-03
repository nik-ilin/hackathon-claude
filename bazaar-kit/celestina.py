"""Celestina: casamentera de nuestro puesto (v15, auto, 0 bps).

Lee los libros públicos (El Rastro y los venues de otros equipos), encuentra parejas de OTROS equipos que se
cruzan o casi se cruzan y redacta un anuncio veraz invitándoles a cerrar en v15 a precio medio, donde la
comisión es 0. El valor que creen entre ellos en nuestro venue puntúa en market-making.

Parejas que busca:
  cross  puja >= oferta de venta del mismo artículo (la comisión del Rastro, 5 % + 1 P, las tiene paradas)
  near   puja < venta, pero el hueco <= comisión del Rastro sobre la venta: a mitad de camino en v15 ambos ganan
  swap   intercambios carta-por-carta complementarios (lo que uno da es lo que el otro pide, y viceversa)

Nunca destaca ofertas nuestras (no podemos operar en nuestro venue), ni ofertas dirigidas (`to`), ni parejas que
ya están en el mismo venue de 0 % (allí se cruzan solas), ni el mismo par más de una vez cada --repeat-ticks.

    python3 celestina.py                      # dry run: imprime el anuncio (solo lecturas GET)
    python3 celestina.py --pairs-json -       # además exporta las parejas en JSON (stdout o fichero)
    python3 celestina.py --loop               # un ciclo por tick, sigue en dry run
    python3 celestina.py --calibrate          # réplica offline sobre intel/market.db (sin red)
    python3 celestina.py --execute --loop     # publica con announce() (BROKER_KEY o STARTER_BROKER_KEY)

Lecturas públicas sin clave (no gastan los 5 req/s de la clave del equipo); `my_offers` usa BAZAAR_KEY si existe,
solo para excluir nuestras ofertas. Con --execute: como mucho un anuncio cada --every ticks (10 por defecto) y un
intervalo mínimo entre peticiones (--min-interval) para no comernos el límite compartido.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from typing import Optional

HERE = os.path.dirname(os.path.abspath(__file__))
URL = os.environ.get("BAZAAR_URL", "https://bazaar.causaprima.ai")
STATE = os.path.join(HERE, "data", "celestina_state.json")
DB = os.path.expanduser("~/bazaar-bot/intel/market.db")
MAX_CHARS = 1200  # el servidor recorta los mensajes a 1.200 caracteres


def rastro_fee(price: int) -> int:
    """Comisión del Rastro sobre un precio: 5 % redondeado arriba + 1 P por carta."""
    return math.ceil(0.05 * max(0, price)) + 1


def token(asset: dict) -> str:
    """'card:RET-06' (o 'pack:sobre_barrio'): la forma en que `want.types` pide un artículo."""
    return f"{asset.get('kind') or 'card'}:{asset.get('ref')}"


def ref_of(tok: str) -> str:
    return tok.split(":", 1)[1] if ":" in tok else tok


# --------------------------------------------------------------------------- normalización

def norm(o: dict, venue: Optional[str] = None, makers: Optional[dict] = None) -> dict:
    """Oferta de board/evento a un dict plano. `makers` traduce id de oferta -> equipo real (del feed)."""
    g, w = o.get("give") or {}, o.get("want") or {}
    pseudo = o.get("maker")
    team = (makers or {}).get(o.get("id")) or (pseudo if _is_team(pseudo) else None)
    return {
        "id": o.get("id"), "venue": o.get("venue") or venue, "maker": team or pseudo, "team": team,
        "to": o.get("to"), "expires": o.get("expires_tick"),
        "give_cash": int(g.get("cash") or 0), "give": [token(a) for a in g.get("assets") or []],
        "give_ids": [a.get("id") for a in g.get("assets") or []],
        "want_cash": int(w.get("cash") or 0), "want": list(w.get("types") or []),
        "want_ids": [a.get("id") if isinstance(a, dict) else a for a in w.get("assets") or []],
    }


def _is_team(m) -> bool:
    return isinstance(m, str) and len(m) == 3 and m[0] == "t" and m[1:].isdigit()


def simple_ask(o: dict) -> Optional[tuple]:
    """(artículo, precio) si la oferta vende exactamente una carta por dinero."""
    if len(o["give"]) == 1 and not o["give_cash"] and o["want_cash"] > 0 and not o["want"] and not o["want_ids"]:
        return o["give"][0], o["want_cash"]
    return None


def simple_bid(o: dict) -> Optional[tuple]:
    """(artículo, precio) si la oferta paga dinero por una copia cualquiera de un artículo."""
    if o["give_cash"] > 0 and not o["give"] and len(o["want"]) == 1 and not o["want_cash"] and not o["want_ids"]:
        return o["want"][0], o["give_cash"]
    return None


def covers(giver: dict, wanter: dict) -> bool:
    """¿Lo que `giver` da satisface lo que `wanter` pide?"""
    if giver["give_cash"] < wanter["want_cash"]:
        return False
    if not set(wanter["want_ids"]) <= set(giver["give_ids"]):
        return False
    have = Counter(giver["give"])
    return all(have[t] >= n for t, n in Counter(wanter["want"]).items())


# --------------------------------------------------------------------------- detección

def eligible(offers: list, *, team: str, own_ids=(), tick: Optional[int] = None, min_ticks_left: int = 2) -> list:
    """Quita nuestras ofertas (por equipo, por id y por el seudónimo con que aparecen), las dirigidas y las que
    caducan antes de que nadie lea el anuncio."""
    own_ids = set(own_ids)
    own_pseudo = {(o["venue"], o["maker"]) for o in offers if o["id"] in own_ids}
    out = []
    for o in offers:
        if o["team"] == team or o["id"] in own_ids or (o["venue"], o["maker"]) in own_pseudo or o["to"]:
            continue
        if tick is not None and o["expires"] is not None and o["expires"] - tick < min_ticks_left:
            continue
        out.append(o)
    return out


def _same_party(a: dict, b: dict) -> bool:
    if a["team"] and b["team"]:
        return a["team"] == b["team"]
    return a["venue"] == b["venue"] and a["maker"] == b["maker"]


def _already_home(a: dict, b: dict, zero_fee: set, our_venue: str) -> bool:
    """Las dos en el mismo venue sin comisión (incluido el nuestro): allí ya se cruzan, no hace falta anunciar."""
    return a["venue"] == b["venue"] and (a["venue"] in zero_fee or a["venue"] == our_venue)


def _side(o: dict, price: Optional[int] = None) -> dict:
    d = {"offer": o["id"], "venue": o["venue"], "maker": o["maker"], "team": o["team"]}
    if price is not None:
        d["price"] = price
    return d


def find_pairs(offers: list, *, our_venue: str = "v15", zero_fee=(), near_extra: int = 0) -> list:
    """Parejas cross/near/swap entre ofertas ya filtradas por `eligible`. Una oferta entra en una sola pareja."""
    zero_fee = set(zero_fee) - {"rastro"}
    pairs, used = [], set()
    asks, bids = defaultdict(list), defaultdict(list)
    for o in offers:
        if (a := simple_ask(o)):
            asks[a[0]].append((a[1], o))
        elif (b := simple_bid(o)):
            bids[b[0]].append((b[1], o))
    for item in sorted(set(asks) & set(bids)):
        bl = sorted(bids[item], key=lambda x: (-x[0], x[1]["id"] or 0))
        for ask, s in sorted(asks[item], key=lambda x: (x[0], x[1]["id"] or 0)):
            limit = rastro_fee(ask) + near_extra
            for bid, b in bl:
                if b["id"] in used or _same_party(s, b) or _already_home(s, b, zero_fee, our_venue):
                    continue
                if ask - bid > limit:
                    break  # las pujas van de mayor a menor: ninguna otra llega
                used.update((s["id"], b["id"]))
                kind = "cross" if bid >= ask else "near"
                pairs.append({"kind": kind, "item": item, "ref": ref_of(item), "sell": _side(s, ask),
                              "buy": _side(b, bid), "gap": ask - bid, "mid": (ask + bid + 1) // 2,
                              "rastro_fee": rastro_fee((ask + bid + 1) // 2),
                              "key": f"{kind}:{item}:{s['maker']}>{b['maker']}"})
                break
    swappers = [o for o in offers if o["give"] and (o["want"] or o["want_ids"]) and o["id"] not in used]
    by_give = defaultdict(list)
    for o in swappers:
        for t in set(o["give"]):
            by_give[t].append(o)
    for a in sorted(swappers, key=lambda o: o["id"] or 0):
        if a["id"] in used or not a["want"]:
            continue
        for b in by_give.get(a["want"][0], []):
            if b["id"] in used or b["id"] == a["id"] or _same_party(a, b) or _already_home(a, b, zero_fee, our_venue):
                continue
            if covers(a, b) and covers(b, a):
                used.update((a["id"], b["id"]))
                x, y = sorted((a, b), key=lambda o: o["id"] or 0)
                pairs.append({"kind": "swap", "a": {**_side(x), "gives": x["give"], "wants": x["want"]},
                              "b": {**_side(y), "gives": y["give"], "wants": y["want"]},
                              "key": "swap:" + "|".join(sorted(f"{o['maker']}:{','.join(sorted(o['give']))}"
                                                                for o in (x, y)))})
                break
    return rank(pairs)


def rank(pairs: list) -> list:
    """Primero las que ya se cruzan (más excedente antes), luego swaps, luego las de hueco pequeño."""
    def key(p):
        if p["kind"] == "cross":
            return (0, -(p["buy"]["price"] - p["sell"]["price"]), p["key"])
        if p["kind"] == "swap":
            return (1, 0, p["key"])
        return (2, p["gap"], p["key"])
    return sorted(pairs, key=key)


# --------------------------------------------------------------------------- anuncio

def _who(side: dict) -> str:
    return side["team"] or f"offer #{side['offer']}"


def _where(v: str) -> str:
    return "El Rastro" if v == "rastro" else v


def fee_text(fee_bps: int, fee_per_card: int) -> str:
    pct = f"{fee_bps / 100:g} %"
    return f"{pct} and {fee_per_card} P per card"


def pair_line(p: dict, our_venue: str) -> str:
    if p["kind"] == "swap":
        a, b = p["a"], p["b"]
        return (f"Swap {'+'.join(map(ref_of, a['gives']))} <-> {'+'.join(map(ref_of, b['gives']))}: "
                f"{_who(a)} ({_where(a['venue'])}) gives {'+'.join(map(ref_of, a['gives']))} for "
                f"{'+'.join(map(ref_of, a['wants']))}; {_who(b)} ({_where(b['venue'])}) gives "
                f"{'+'.join(map(ref_of, b['gives']))} for {'+'.join(map(ref_of, b['wants']))}. "
                f"Post the swap on {our_venue}.")
    s, b = p["sell"], p["buy"]
    head = (f"{p['ref']}: {_who(b)} bids {b['price']} ({_where(b['venue'])}), "
            f"{_who(s)} asks {s['price']} ({_where(s['venue'])})")
    if p["kind"] == "cross":
        return f"{head}. Meet at {p['mid']} on {our_venue}."
    return (f"{head}, {p['gap']} P apart; El Rastro's fee at {p['mid']} is {p['rastro_fee']} P. "
            f"Meet at {p['mid']} on {our_venue}.")


def compose(pairs: list, *, venue: str = "v15", fee_bps: int = 0, fee_per_card: int = 0,
            max_pairs: int = 6, max_chars: int = MAX_CHARS) -> tuple:
    """(texto, parejas incluidas). Solo datos de `pairs`; corta antes de pasar de `max_chars`."""
    if not pairs:
        return "", []
    head = (f"{venue} matchmaking (Team 15 stall: auto-crossed every tick, {fee_text(fee_bps, fee_per_card)}). "
            f"Pairs on the books now that El Rastro's 5 % + 1 P keeps apart:")
    tail = (f"Bid = give cash, want {{\"cards\": [REF]}}; swaps = give your card, want theirs. "
            f"On {venue} the accepting side pays {fee_text(fee_bps, fee_per_card)}.")
    lines, used = [head], []
    for p in pairs[:max_pairs]:
        line = pair_line(p, venue)
        if len(" ".join(lines + [line, tail])) > max_chars:
            continue
        lines.append(line)
        used.append(p)
    if not used:
        return "", []
    text = " ".join(lines + [tail])
    if len(text) > max_chars:  # el pie no cabe: mejor sin pie que recortado
        text = " ".join(lines)
    return text[:max_chars], used


# --------------------------------------------------------------------------- ritmo y memoria

def load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            s = json.load(f)
        return {"last_tick": s.get("last_tick"), "pairs": dict(s.get("pairs") or {})}
    except (OSError, ValueError):
        return {"last_tick": None, "pairs": {}}


def save_state(path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def fresh(pairs: list, state: dict, tick: int, repeat_ticks: int) -> list:
    """Parejas no anunciadas en los últimos `repeat_ticks`."""
    seen = state.get("pairs") or {}
    return [p for p in pairs if seen.get(p["key"]) is None or tick - seen[p["key"]] >= repeat_ticks]


def may_announce(state: dict, tick: int, every: int) -> bool:
    last = state.get("last_tick")
    return last is None or tick - last >= every


def record(state: dict, used: list, tick: int, repeat_ticks: int) -> dict:
    pairs = {k: t for k, t in (state.get("pairs") or {}).items() if tick - t < max(repeat_ticks, 1) * 4}
    pairs.update({p["key"]: tick for p in used})
    return {"last_tick": tick, "pairs": pairs}


# --------------------------------------------------------------------------- lectura en vivo (solo GET)

class Throttle:
    """Intervalo mínimo entre peticiones: deja sitio al resto de procesos que comparten el límite de 5 req/s."""

    def __init__(self, min_interval: float):
        self.min_interval, self._last = min_interval, 0.0

    def __call__(self, fn, *a, **kw):
        wait = self._last + self.min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            return fn(*a, **kw)
        finally:
            self._last = time.monotonic()


def public_client(url: str):
    """Cliente sin cabecera de clave: las lecturas públicas no gastan el límite por clave del equipo."""
    from bazaar_sdk import Bazaar, _Http
    c = Bazaar.__new__(Bazaar)
    _Http.__init__(c, url, {}, 15.0, False, 3)
    c.key = ""
    return c


def _list(x, key: str) -> list:
    if isinstance(x, list):
        return x
    return (x or {}).get(key) or []


def makers_from_feed(events: list) -> dict:
    """id de oferta -> equipo real, de los eventos offer.listed del feed público."""
    out = {}
    for e in events:
        if e.get("type") == "offer.listed":
            o = (e.get("payload") or {}).get("offer") or {}
            if o.get("id") is not None and _is_team(o.get("maker")):
                out[o["id"]] = o["maker"]
    return out


def makers_from_db(path: Optional[str]) -> dict:
    """id de oferta -> equipo, de la tabla offers del colector (solo lectura); {} si no hay base."""
    if not path or not os.path.exists(path):
        return {}
    try:
        return dict(_db(path).execute("SELECT id, maker FROM offers WHERE maker LIKE 't%'").fetchall())
    except sqlite3.Error:
        return {}


def snapshot(pub, call, *, our_venue: str, scan: Optional[list], team_api=None, known: Optional[dict] = None) -> dict:
    """Todo lo que hace falta para un ciclo, con lecturas GET. `known`: id de oferta -> equipo ya sabido."""
    tick = int(call(pub.clock)["tick"])
    venues = _list(call(pub.venues), "venues")
    info = {v.get("venue"): v for v in venues}
    names = scan or ["rastro"] + [v["venue"] for v in venues if v.get("venue") not in (None, "rastro", our_venue)
                                  and v.get("status", "open") == "open"]
    makers = {**(known or {}), **makers_from_feed(_list(call(pub.feed, 500), "events"))}
    offers = []
    for v in names:
        try:
            for o in _list(call(pub.board, v), "offers"):
                offers.append(norm(o, v, makers))
        except Exception as e:  # un venue cerrado o caído no para el resto
            print(f"[celestina] no se pudo leer {v}: {e}", file=sys.stderr)
    own_ids, team = [], None
    if team_api is not None:
        try:
            team = (call(team_api.me) or {}).get("id")
            own_ids = [o["id"] for o in _list(call(team_api.my_offers), "offers") if o.get("id") is not None]
        except Exception as e:
            print(f"[celestina] sin my_offers ({e}): excluyo solo por equipo", file=sys.stderr)
    zero = {k for k, v in info.items() if k and not v.get("fee_bps") and not v.get("fee_per_card")}
    return {"tick": tick, "offers": offers, "own_ids": own_ids, "team": team, "zero_fee": zero,
            "ours": info.get(our_venue)}


def cycle(snap: dict, args, state: dict) -> dict:
    """Detecta y redacta. Devuelve {pairs, text, used, reason}; no escribe nada."""
    team = snap.get("team") or args.team
    offers = eligible(snap["offers"], team=team, own_ids=snap["own_ids"], tick=snap["tick"],
                      min_ticks_left=args.min_ticks_left)
    pairs = find_pairs(offers, our_venue=args.venue, zero_fee=snap["zero_fee"], near_extra=args.near_extra)
    ours = snap.get("ours")
    if ours is None:
        return {"pairs": pairs, "text": "", "used": [], "reason": f"{args.venue} no aparece en /api/venues"}
    if ours.get("status", "open") != "open":
        return {"pairs": pairs, "text": "", "used": [], "reason": f"{args.venue} no está abierto"}
    fee_bps, per_card = int(ours.get("fee_bps") or 0), int(ours.get("fee_per_card") or 0)
    text, used = compose(fresh(pairs, state, snap["tick"], args.repeat_ticks), venue=args.venue, fee_bps=fee_bps,
                         fee_per_card=per_card, max_pairs=args.max_pairs)
    reason = "" if text else ("sin parejas" if not pairs else "todas las parejas anunciadas hace poco")
    return {"pairs": pairs, "text": text, "used": used, "reason": reason}


def export(pairs: list, path: str, tick: Optional[int]) -> None:
    data = json.dumps({"tick": tick, "pairs": pairs}, ensure_ascii=False, indent=1)
    if path == "-":
        print(data)
    else:
        with open(path, "w", encoding="utf-8") as f:
            f.write(data)


def run_live(args) -> int:
    call = Throttle(args.min_interval)
    pub = public_client(args.url)
    team_api = None
    if os.environ.get("BAZAAR_KEY"):
        from bazaar_sdk import Bazaar
        team_api = Bazaar(args.url, os.environ["BAZAAR_KEY"], wait_on_tick=False)
    broker = None
    if args.execute:
        bk = os.environ.get("BROKER_KEY") or os.environ.get("STARTER_BROKER_KEY")
        if not bk:
            print("--execute necesita BROKER_KEY o STARTER_BROKER_KEY en el entorno", file=sys.stderr)
            return 2
        from bazaar_sdk import Broker
        broker = Broker(args.url, bk)
    last_tick = None
    while True:
        snap = snapshot(pub, call, our_venue=args.venue, scan=args.scan, team_api=team_api,
                        known=makers_from_db(args.makers_db))
        if snap["tick"] != last_tick:
            last_tick = snap["tick"]
            state = load_state(args.state)
            res = cycle(snap, args, state)
            print(f"[celestina] tick {snap['tick']}: {len(snap['offers'])} ofertas leídas, "
                  f"{len(res['pairs'])} parejas ({Counter(p['kind'] for p in res['pairs'])})")
            if args.pairs_json:
                export(res["pairs"], args.pairs_json, snap["tick"])
            if not res["text"]:
                print(f"[celestina] sin anuncio: {res['reason']}")
            elif not may_announce(state, snap["tick"], args.every):
                print(f"[celestina] anuncio listo pero último hace {snap['tick'] - state['last_tick']} ticks "
                      f"(< {args.every}): espero\n{res['text']}")
            elif broker is None:
                print(f"[celestina] DRY RUN ({len(res['text'])} caracteres):\n{res['text']}")
            else:
                try:
                    call(broker.announce, res["text"])
                    save_state(args.state, record(state, res["used"], snap["tick"], args.repeat_ticks))
                    print(f"[celestina] anunciado en tick {snap['tick']}:\n{res['text']}")
                except Exception as e:  # una negativa del servidor no debe tumbar el bucle
                    print(f"[celestina] announce rechazado: {e}", file=sys.stderr)
        if not args.loop:
            return 0
        time.sleep(args.poll)


# --------------------------------------------------------------------------- calibración offline

def _db(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def replay_books(db: sqlite3.Connection, t0: int, t1: int) -> dict:
    """Libro público reconstruido a cada tick de [t0, t1] desde events (offer.listed / offer.cancelled /
    settlement). Una venta muere cuando su carta cambia de manos; una puja, cuando su equipo recibe esa carta en
    ese venue."""
    rows = db.execute("SELECT tick, type, payload FROM events WHERE type IN ('offer.listed','offer.cancelled',"
                      "'settlement') AND tick <= ? ORDER BY id", (t1,)).fetchall()
    live, books = {}, {}
    by_tick = defaultdict(list)
    for tick, typ, payload in rows:
        by_tick[tick].append((typ, json.loads(payload)))
    for tick in range(min(by_tick or [t0]), t1 + 1):
        for typ, p in by_tick.get(tick, []):
            if typ == "offer.listed":
                o = p.get("offer") or {}
                if o.get("id") is not None and o.get("thread") is None:
                    live[o["id"]] = norm(o, p.get("venue"))
            elif typ == "offer.cancelled":
                live.pop(p.get("offer") if isinstance(p.get("offer"), int) else (p.get("offer") or {}).get("id"),
                         None)
            elif typ == "settlement":
                for it in p.get("items") or []:
                    for oid, o in list(live.items()):
                        if it.get("id") in o["give_ids"]:
                            live.pop(oid)
                        elif (o["team"] == it.get("to") and o["venue"] == p.get("venue")
                              and token(it) in o["want"] and simple_bid(o)):
                            live.pop(oid)
                            break
        for oid, o in list(live.items()):
            if o["expires"] is not None and o["expires"] <= tick:
                live.pop(oid)
        if tick >= t0:
            books[tick] = list(live.values())
    return books


def snapshot_books(db: sqlite3.Connection, since_ts: float) -> dict:
    """Libros tal como los vio el colector (board_snapshots, con seudónimos), por instante."""
    makers = dict(db.execute("SELECT id, maker FROM offers WHERE maker LIKE 't%'").fetchall())
    books = defaultdict(list)
    for ts, tick, venue, oid, maker, too, gc, ga, gt, wc, wt, ct, et in db.execute(
            "SELECT * FROM board_snapshots WHERE snap_ts >= ?", (since_ts,)):
        o = {"id": oid, "maker": maker, "to": too or None, "venue": venue, "expires_tick": et,
             "give": {"cash": gc, "assets": json.loads(ga or "[]")},
             "want": {"cash": wc, "types": json.loads(wt or "[]")}}
        books[(ts, tick)].append(norm(o, venue, makers))
    return books


def _zero_fee_from_db(db: sqlite3.Connection) -> set:
    row = db.execute("SELECT payload FROM json_snapshots WHERE kind='venues' ORDER BY snap_ts DESC LIMIT 1").fetchone()
    vs = _list(json.loads(row[0]), "venues") if row else []
    return {v["venue"] for v in vs if v.get("venue") and not v.get("fee_bps") and not v.get("fee_per_card")}


def calibrate(args) -> dict:
    db = _db(args.db)
    zero = _zero_fee_from_db(db)
    t1 = db.execute("SELECT MAX(tick) FROM events").fetchone()[0]
    t0 = t1 - int(args.hours * 3600 / args.tick_seconds) + 1
    out = {"ticks": [t0, t1], "zero_fee_venues": sorted(zero)}
    for name, books in (("events", {(None, t): b for t, b in replay_books(db, t0, t1).items()}),
                        ("board_snapshots", snapshot_books(
                            db, db.execute("SELECT MAX(snap_ts) FROM board_snapshots").fetchone()[0] - args.hours * 3600))):
        uniq, per_tick, kinds, examples = {}, [], Counter(), []
        state = {"last_tick": None, "pairs": {}}
        announced = 0
        for (ts, tick), offers in sorted(books.items(), key=lambda kv: (kv[0][1], kv[0][0] or 0)):
            el = eligible(offers, team=args.team, tick=tick, min_ticks_left=args.min_ticks_left)
            pairs = find_pairs(el, our_venue=args.venue, zero_fee=zero, near_extra=args.near_extra)
            per_tick.append(len(pairs))
            for p in pairs:
                if p["key"] not in uniq:
                    uniq[p["key"]] = p
                    kinds[p["kind"]] += 1
            if may_announce(state, tick, args.every):
                text, used = compose(fresh(pairs, state, tick, args.repeat_ticks), venue=args.venue,
                                     max_pairs=args.max_pairs)
                if text:
                    announced += 1
                    state = record(state, used, tick, args.repeat_ticks)
                    if len(examples) < 2:
                        examples.append({"tick": tick, "text": text})
        out[name] = {"books": len(books), "unique_pairs": len(uniq), "by_kind": dict(kinds),
                     "mean_pairs_per_book": round(sum(per_tick) / len(per_tick), 2) if per_tick else 0,
                     "books_with_pairs": sum(1 for n in per_tick if n), "announcements": announced,
                     "examples": examples, "top": [pair_line(p, args.venue) for p in rank(list(uniq.values()))[:8]]}
    return out


# --------------------------------------------------------------------------- CLI

def parse(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default=URL)
    p.add_argument("--venue", default="v15", help="nuestro venue (destino de las parejas)")
    p.add_argument("--team", default="t15", help="nuestro equipo (se excluyen sus ofertas); con BAZAAR_KEY se lee de /api/me")
    p.add_argument("--scan", type=lambda s: [x for x in s.split(",") if x], default=None,
                   help="venues a leer (por defecto El Rastro y todos los abiertos salvo el nuestro)")
    p.add_argument("--execute", action="store_true", help="publicar con announce() (por defecto solo imprime)")
    p.add_argument("--loop", action="store_true", help="repetir cada tick")
    p.add_argument("--poll", type=float, default=5.0, help="segundos entre lecturas del reloj en --loop")
    p.add_argument("--every", type=int, default=10, help="mínimo de ticks entre anuncios")
    p.add_argument("--repeat-ticks", type=int, default=60, help="no repetir el mismo par antes de N ticks")
    p.add_argument("--max-pairs", type=int, default=6)
    p.add_argument("--near-extra", type=int, default=0, help="P extra de hueco admitido sobre la comisión del Rastro")
    p.add_argument("--min-ticks-left", type=int, default=2, help="ignorar ofertas que caducan antes")
    p.add_argument("--min-interval", type=float, default=0.4, help="segundos mínimos entre peticiones")
    p.add_argument("--pairs-json", default=None, help="exportar parejas (ruta o - para stdout)")
    p.add_argument("--state", default=STATE)
    p.add_argument("--calibrate", action="store_true", help="réplica offline sobre market.db (sin red)")
    p.add_argument("--db", default=DB)
    p.add_argument("--makers-db", default=DB, help="market.db para poner nombre de equipo a ofertas que el feed ya "
                                                    "no muestra ('' para no usarla)")
    p.add_argument("--hours", type=float, default=1.0)
    p.add_argument("--tick-seconds", type=float, default=30.0, help="duración del tick en la réplica")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse(argv)
    if args.calibrate:
        if args.execute:
            print("--calibrate es offline: ignoro --execute", file=sys.stderr)
        print(json.dumps(calibrate(args), ensure_ascii=False, indent=1))
        return 0
    return run_live(args)


if __name__ == "__main__":
    sys.exit(main())
