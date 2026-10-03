"""Mide cómo ceden los vendedores en los hilos reales (SOLO LECTURA de la base del recolector; no copia datos al repo).

    python3 ladder_measure.py --db ~/bazaar-bot/intel/market.db                 # tabla por vendedor × modo × rareza
    python3 ladder_measure.py --db ... --rows /tmp/hilos.json                    # hilos para dealer_sim.py --replay
    python3 ladder_measure.py --db ... --bluffs                                  # faroles: «final» sin final:true

Por hilo: apertura del vendedor, su oferta `final: true`, precio del trato (liquidación del mismo equipo y vendedor
dentro del hilo; en ventas, solo si liquida exactamente la copia del tema; en compras, solo si la carta llega al equipo), apertura y paso medianos del equipo.
"""
from __future__ import annotations

import argparse
import collections
import json
import sqlite3
import statistics

import ladder_calibrated as lcal

DEALERS = set(lcal.LADDER_DEALERS)


def med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def load(db: str) -> list:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    q = lambda s: con.execute(s).fetchall()
    topics = {tid: json.loads(t) if t else None for tid, t in q("SELECT id, topic FROM threads")}
    rar_asset, ref_asset, rar_ref = {}, {}, {}
    for table in ("settlement_items", "provenance"):
        for aid, ref, rar in q(f"SELECT asset_id, ref, rarity FROM {table}"):
            if rar:
                rar_asset[aid], rar_ref[ref] = rar, rar
            if ref:
                ref_asset[aid] = ref
    msgs = collections.defaultdict(list)
    for tid, tick, team, w, sender, text, price, final in q(
            "SELECT thread, tick, team, with_, sender, text, price, final FROM thread_messages WHERE kind='persona' "
            "ORDER BY thread, tick, id"):
        msgs[tid].append({"tick": tick, "team": team, "with": w, "sender": sender, "text": text or "",
                          "price": price, "final": bool(final)})
    sett = collections.defaultdict(list)
    for sid, tick, persona, a, b, price in q("SELECT id, tick, persona, party_a, party_b, price FROM settlements "
                                             "WHERE persona IS NOT NULL"):
        sett[(a if a != persona else b, persona)].append((tick, price, sid))
    items, to_team = collections.defaultdict(set), collections.defaultdict(set)
    for sid, aid, too in q("SELECT settlement, asset_id, too FROM settlement_items"):
        items[sid].add(aid)
        to_team[sid].add(too)
    rows = []
    for tid, ms in msgs.items():
        team = next((m["team"] for m in ms if m["team"]), None)
        dealer = next((m["with"] for m in ms if m["with"] in DEALERS), None)
        if not team or not dealer:
            continue
        topic = topics.get(tid) or {}
        mode = "sell" if "sell" in topic else "buy" if "buy" in topic else "?"
        ref, ids = None, []
        if mode == "sell":
            ids = list(topic["sell"].get("assets") or [])
            kind = rar_asset.get(ids[0], "?") if len(ids) == 1 else f"lote{len(ids)}"
            ref = ref_asset.get(ids[0]) if len(ids) == 1 else None
        elif mode == "buy":
            b = topic["buy"]
            ref = b.get("card")
            kind = b.get("pack") or b.get("rarity") or rar_ref.get(ref, "?")
        else:
            kind = "?"
        dp = [m for m in ms if m["sender"] == dealer and m["price"] is not None]
        tp = [m["price"] for m in ms if m["sender"] == team and m["price"] is not None]
        deal = None
        for tick, price, sid in sett.get((team, dealer), []):
            ok = items.get(sid) == set(ids) if mode == "sell" else team in to_team.get(sid, ())  # compra: la carta llega
            if ms[0]["tick"] <= tick <= ms[-1]["tick"] + 3 and ok:
                deal = price
        set_id = (ref or "")[:3] or None
        rows.append({"thread": tid, "team": team, "dealer": dealer, "mode": mode, "kind": kind, "ref": ref,
                     "fav": lcal.is_fav(dealer, set_id), "dealer_open": dp[0]["price"] if dp else None,
                     "dealer_prices": [m["price"] for m in dp],
                     "final": next((m["price"] for m in dp if m["final"]), None),
                     "bluffs": [m["price"] for m in dp if not m["final"] and lcal.BLUFF_RE.search(m["text"])],
                     "team_prices": tp, "team_open": tp[0] if tp else None,
                     "team_step": med([abs(b - a) for a, b in zip(tp, tp[1:])]), "rounds": len(tp), "deal": deal})
    return rows


def table(rows: list) -> list:
    g = collections.defaultdict(list)
    for r in rows:
        g[(r["dealer"], r["mode"], r["kind"] + ("|fav" if r["fav"] else ""))].append(r)
    out = []
    for (dealer, mode, kind), rs in sorted(g.items()):
        deals = [r for r in rs if r["deal"] is not None]
        best = (min if mode == "buy" else max)(deals, key=lambda r: r["deal"]) if deals else None
        conc = [abs(r["dealer_prices"][-1] - r["dealer_prices"][0]) / r["rounds"] for r in rs
                if r["rounds"] and len(r["dealer_prices"]) > 1]
        to_final = [sum(1 for _ in r["team_prices"]) for r in rs if r["final"] is not None]
        out.append({"dealer": dealer, "mode": mode, "kind": kind, "hilos": len(rs), "tratos": len(deals),
                    "apertura": med([r["dealer_open"] for r in rs]), "final_true": med([r["final"] for r in rs]),
                    "trato_mediano": med([r["deal"] for r in deals]), "mejor": best and best["deal"],
                    "mejor_por": best and {"equipo": best["team"], "abre": best["team_open"],
                                           "paso": best["team_step"], "rondas": best["rounds"]},
                    "cesion_ronda": round(med(conc), 2) if conc else None,
                    "rondas_hasta_final": med(to_final)})
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True, help="market.db del recolector (se abre en solo lectura)")
    ap.add_argument("--rows", help="escribir aquí los hilos (JSON) para dealer_sim.py --replay")
    ap.add_argument("--bluffs", action="store_true", help="listar faroles («final» en el texto sin final:true)")
    a = ap.parse_args()
    rows = load(a.db)
    if a.rows:
        with open(a.rows, "w", encoding="utf-8") as fh:
            json.dump(rows, fh)
    print(f"{len(rows)} hilos con vendedores")
    for t in table(rows):
        b = t["mejor_por"] or {}
        print(f"{t['dealer']:6s} {t['mode']:4s} {t['kind']:16s} hilos {t['hilos']:3d} tratos {t['tratos']:3d} · "
              f"apertura {t['apertura']} · final:true {t['final_true']} · trato mediano {t['trato_mediano']} · "
              f"mejor {t['mejor']} ({b.get('equipo')} abre {b.get('abre')} paso {b.get('paso')}, "
              f"{b.get('rondas')} rondas) · cesión/ronda {t['cesion_ronda']} · rondas hasta final {t['rondas_hasta_final']}")
    if a.bluffs:
        for r in rows:
            if r["bluffs"]:
                after = r["dealer_prices"][r["dealer_prices"].index(r["bluffs"][0]) + 1:] if r["bluffs"][0] in \
                    r["dealer_prices"] else []
                print(f"farol hilo {r['thread']} {r['team']}→{r['dealer']}: «final» a {r['bluffs']} sin final:true; "
                      f"después {after} · trato {r['deal']}")


if __name__ == "__main__":
    main()
