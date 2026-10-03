#!/usr/bin/env python3
"""Recolecta el feed público y publica un informe de negociación.

Add-on independiente: no importa ni modifica nada del repo. Sólo lee.
Nunca escribe en el juego, nunca negocia, nunca gasta cuota.

    python3 feed_watch.py                 # una pasada y el informe
    python3 feed_watch.py --watch         # recoge cada tick hasta Ctrl-C
    python3 feed_watch.py --card RET-09   # todo lo que se sabe de una carta
    python3 feed_watch.py --report        # sólo lo ya guardado, sin red

El feed sólo devuelve los últimos 500 eventos (~25 ticks, ~12 min a 30 s).
Lo que no se recoja se pierde: de ahí `--watch`.

La clave de equipo **no** hace falta: `/api/feed` es público. Si existe
`BAZAAR_KEY` se envía, porque el feed autenticado puede incluir eventos
dirigidos a nosotros, pero su ausencia no impide nada.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from feed_oracle import Oracle, is_team

HERE = Path(__file__).resolve().parent
STORE = HERE / "data" / "feed_history.jsonl"
DEFAULT_URL = os.environ.get("BAZAAR_URL", "https://bazaar.causaprima.ai")


# ------------------------------------------------------------------- red

def _get(url: str, path: str, key: str | None, timeout: float = 20.0) -> dict:
    req = urllib.request.Request(url.rstrip("/") + path)
    if key:
        req.add_header("X-Team-Key", key)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_feed(url: str, key: str | None, limit: int = 500) -> list[dict]:
    try:
        return _get(url, f"/api/feed?limit={limit}", key).get("events") or []
    except urllib.error.HTTPError as e:
        # el feed autenticado puede rechazar una clave mala; el público no
        if e.code in (401, 403) and key:
            print("  (clave rechazada; sigo con el feed público)", file=sys.stderr)
            return _get(url, f"/api/feed?limit={limit}", None).get("events") or []
        raise


def fetch_catalog(url: str, key: str | None) -> dict:
    """`/api/catalog` es público: rareza, book, tirada y acuñadas exactas."""
    try:
        return _get(url, "/api/catalog", key)
    except Exception as exc:
        print(f"  (sin catálogo: {exc})", file=sys.stderr)
        return {}


def fetch_clock(url: str, key: str | None) -> dict:
    try:
        return _get(url, "/api/clock", key)
    except Exception:
        return {}


# --------------------------------------------------------------- persistencia

def load_store(path: Path = STORE) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue          # una línea truncada por un Ctrl-C no invalida el resto
    return out


def append_store(events: list[dict], known: set[int], path: Path = STORE) -> int:
    """Añade sólo los eventos cuyo `id` no esté ya guardado."""
    new = [e for e in events if e.get("id") is not None and e["id"] not in known]
    if not new:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for e in new:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")
            known.add(e["id"])
    return len(new)


# ----------------------------------------------------------------- informes

def report(o: Oracle, *, top: int = 12) -> None:
    c = o.coverage
    print(f"\ncobertura: {c['events']} eventos · ticks {c['tick_min']}-{c['tick_max']} "
          f"({c['ticks']}) · {c['cards']} cartas · {c['settled']} liquidaciones")

    print("\n== PRECIOS OBSERVADOS DE DEALER (cotizaciones y liquidaciones separadas; no suelos garantizados) ==")
    print(f"{'dealer':8} {'carta':8} {'lado':5} {'abre':>5} {'suelo':>6} {'pasos':>12} "
          f"{'n':>3}  conf")
    lines = sorted(o.dealers.values(), key=lambda d: (-d.n, d.dealer, d.ref))
    for dl in lines[:top]:
        steps = ",".join(str(s) for s in dl.steps[:5]) or "-"
        print(f"{dl.dealer:8} {dl.ref:8} {dl.side:5} {str(dl.opening):>5} "
              f"{str(dl.floor):>6} {steps:>12} {dl.n:>3}  {dl.confidence}")
    if len(lines) > top:
        print(f"   … y {len(lines) - top} líneas más (--top para ver más)")

    print("\n== MERCADO ENTRE EQUIPOS ==")
    print(f"{'carta':8} {'rar':9} {'justo':>6} {'liquid.':>14} {'asks':>12} "
          f"{'bajar a':>8}  conf")
    cards = sorted(o.cards.values(),
                   key=lambda c: (-len(c.settled), -len(c.asks), c.ref))
    for cm in cards[:top]:
        fair = f"{cm.fair:.1f}" if cm.fair is not None else "-"
        sett = ",".join(map(str, cm.settled[:4])) or "-"
        asks = f"{min(cm.asks)}-{max(cm.asks)}" if cm.asks else "-"
        print(f"{cm.ref:8} {str(cm.rarity):9} {fair:>6} {sett:>14} {asks:>12} "
              f"{str(cm.undercut()):>8}  {cm.confidence}")
    if len(cards) > top:
        print(f"   … y {len(cards) - top} cartas más")

    arb = o.arbitrage()
    print(f"\n== ARBITRAJE: más barato en un equipo que en el dealer ({len(arb)}) ==")
    if not arb:
        print("  nada ahora mismo")
    for a in arb[:top]:
        print(f"  {a['ref']:8} equipo pide {a['team_ask']:>3} P  vs  "
              f"{a['dealer']} suelo {a['dealer_floor']:>3} P   "
              f"ahorro {a['saving']:>3} P   tienen: {','.join(a['holders']) or '?'}")

    prem = o.premium_buyers()
    if prem:
        print(f"\n== QUIÉN PAGA SOBREPRECIO (a quién venderle caro) ({len(prem)}) ==")
        for r in prem[:top]:
            print(f"  {r['team']} pagó {r['paid']:>3} P por {r['ref']} "
                  f"({r['rarity']}, book {r['book']} P) = x{r['multiple']}  "
                  f"→ le falta cerrar {r['set']}")
        hot = o.hot_sets()
        if hot:
            print("  sets calientes: " + ", ".join(f"{k} ({v})" for k, v in hot.items()))

    if o.responses:
        print("\n== CUÁNTO CUESTA MOVER A CADA DEALER ==")
        print(f"{'dealer':8} {'ratio':>6} {'paso':>5} {'rondas':>7} {'cedí y no movió':>16} {'n':>4}")
        for r in sorted(o.responses.values(), key=lambda x: -x.n):
            pa = o.patience.get(r.dealer)
            rounds = pa.typical if pa and pa.typical else (
                f">{pa.seen_longer}" if pa and pa.seen_longer else "-")
            ratio = f"{r.ratio:.2f}" if r.ratio else "-"
            print(f"{r.dealer:8} {ratio:>6} {str(r.best_step):>5} {str(rounds):>7} "
                  f"{r.wasted:>16} {r.n:>4}")
        print("  ratio = primas que cede el dealer por cada prima que cedes.")
        print("  paso  = el tamaño de concesión que mejor le funcionó a quien regateó.")

    rar = [(r, o.rarity_floor(r)) for r in ("common", "uncommon", "rare", "epic", "legendary")]
    rar = [(r, v) for r, v in rar if v is not None]
    if rar:
        print("\n== SUELO TÍPICO POR RAREZA (para una carta nunca vista) ==")
        print("  " + " · ".join(f"{r}: {v:.0f} P" for r, v in rar))


def card_detail(o: Oracle, ref: str) -> None:
    ref = ref.upper()
    cm = o.cards.get(ref)
    print(f"\n=== {ref} ===")
    if cm:
        extra = []
        if cm.book:
            extra.append(f"book {cm.book} P")
        if cm.minted is not None and cm.print_run:
            extra.append(f"acuñadas {cm.minted}/{cm.print_run}")
        if cm.scarcity is not None:
            extra.append(f"escasez {cm.scarcity:.0%}")
        if extra:
            print("catálogo: " + " · ".join(extra))
        print(f"rareza: {cm.rarity}   precio justo: "
              f"{f'{cm.fair:.1f} P' if cm.fair is not None else 'desconocido'} "
              f"({cm.confidence})")
        print(f"liquidado entre equipos: {cm.settled or '-'}")
        print(f"piden (asks): {sorted(cm.asks) or '-'}")
        print(f"ofrecen (bids): {sorted(cm.bids) or '-'}")
        print(f"la tienen: {', '.join(sorted(cm.holders)) or '?'}")
        print(f"la buscan: {', '.join(sorted(cm.seekers)) or '?'}")
        if cm.undercut():
            print(f"para ser el más barato del tablón: {cm.undercut()} P")
    else:
        print("sin datos entre equipos todavía")

    rows = [dl for dl in o.dealers.values() if dl.ref == ref]
    if rows:
        print("\ndealers:")
        for dl in rows:
            verbo = "nos la vende" if dl.side == "ask" else "nos la compra"
            print(f"  {dl.dealer:8} {verbo:14} abre {dl.opening} → suelo {dl.floor} "
                  f"  pasos {dl.steps or '-'}   n={dl.n}  {dl.confidence}")
            a = o.advise(dl.dealer, ref, side=dl.side)
            print(f"           abre tú en {a['open_at']} P · paso {a['step']} · "
                  f"ratio {a['ratio']} · aguanta ~{a['rounds_before_final']} rondas")
            if a["never_accept"]:
                print(f"           NO aceptes {a['never_accept']} P: "
                      f"su apertura no puntúa en la escalera")
    else:
        print("\nningún dealer observado con esta carta todavía")


# --------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--watch", action="store_true", help="recoger en bucle hasta Ctrl-C")
    ap.add_argument("--every", type=float, default=0.0,
                    help="segundos entre pasadas (0 = usar tick_seconds del servidor)")
    ap.add_argument("--report", action="store_true", help="sólo lo guardado, sin red")
    ap.add_argument("--card", help="detalle de una referencia")
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--json", metavar="RUTA", nargs="?", const="data/oracle.json",
                    help="volcar el conocimiento a JSON para quien negocia")
    ap.add_argument("--advise", nargs=2, metavar=("DEALER", "CARTA"),
                    help="consejo concreto para una negociación")
    ap.add_argument("--no-catalog", action="store_true",
                    help="no enriquecer con /api/catalog")
    a = ap.parse_args(argv)

    key = os.environ.get("BAZAAR_KEY") or None
    if key in ("tk-xxxx-xxxx", ""):
        key = None

    o = Oracle()
    if not a.no_catalog and not a.report:
        n = o.load_catalog(fetch_catalog(a.url, key))
        if n:
            print(f"catálogo: {n} referencias con rareza, book y acuñadas")
    stored = load_store()
    known = {e["id"] for e in stored if e.get("id") is not None}
    o.ingest(stored)
    if stored:
        print(f"guardado: {len(stored)} eventos en {STORE.relative_to(HERE)}")

    if not a.report:
        pause = a.every
        if pause <= 0:
            pause = float(fetch_clock(a.url, key).get("tick_seconds") or 30.0)
        while True:
            try:
                events = fetch_feed(a.url, key, a.limit)
            except Exception as exc:               # red inestable: reintentar, no morir
                print(f"  fallo al leer el feed: {exc}", file=sys.stderr)
                if not a.watch:
                    return 1
                time.sleep(pause)
                continue
            added = append_store(events, known)
            o.ingest(events)
            c = o.coverage
            print(f"tick {c['tick_max']}: +{added} nuevos  "
                  f"(total {c['events']}, {c['settled']} liquidaciones)")
            if not a.watch:
                break
            try:
                time.sleep(pause)
            except KeyboardInterrupt:
                print("\ndetenido")
                break

    if a.advise:
        dealer, ref = a.advise[0], a.advise[1].upper()
        adv = o.advise(dealer, ref)
        print(f"\n=== negociar {ref} con {dealer} ===")
        for k, v in adv.items():
            print(f"  {k:20} {v}")
    elif a.card:
        card_detail(o, a.card)
    else:
        report(o, top=a.top)

    if a.json:
        out = Path(a.json)
        if not out.is_absolute():
            out = HERE / out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(o.to_json(), ensure_ascii=False, indent=2),
                       encoding="utf-8")
        print(f"\nJSON para quien negocia: {out}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
