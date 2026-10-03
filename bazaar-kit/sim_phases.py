"""Simulación de las tres fases sobre snapshots EQUIVALENTES (mismo inventario, ofertas y caja; solo cambia la hora).

    python3 sim_phases.py
"""
import contextlib
import io

import phases as ph
import test_phases as tp
from test_market_intel import TEAM, ask, bid


def snapshot():
    boards = [bid(801, "v02", "MAL-02", 20, maker="t07", exp=900),        # venta rentable de un duplicado
              ask(802, "v02", 90, "MAL-10", 28, maker="t09", exp=900)]    # compra que completa Malasaña
    mine = [bid(900, "v02", "RET-02", 25, maker=TEAM, created=900, exp=2000)]  # puja pasiva que inmoviliza 25 P
    return tp.world(cash=120, boards=boards, mine=mine)


def main():
    rows = []
    for label, now in (("A · operación activa", "19:00"), ("B · transición", "22:10"), ("C · tesorería", "22:45")):
        with contextlib.redirect_stdout(io.StringIO()):
            cands, pl, _ = tp.run(snapshot(), now)
        p = pl["phase"]
        valid = [c for c in cands if not c.get("blockers") and c["type"] != "info"]
        rows.append((label, p, cands, valid))
    for label, p, cands, valid in rows:
        print(f"\n=== {label} · faltan {p['minutes_to_close']:.0f} min · w={p['w']} · libre {p['scenarios']['confirmed']} P "
              f"· déficit a 150 P: {p['deficit_min']} P ===")
        for c in sorted(valid, key=lambda c: -c.get("score", 0))[:6]:
            tag = "VENTA" if ph.is_sale(c) else "COMPRA" if ph.purchase_cost(c) else c["type"].upper()
            print(f"   OK   {tag:<7} {c['type']:<7} {c.get('ref') or c.get('offer')} · {c.get('kind')}"
                  + (f" · caduca {c['expires_in']}" if c.get("expires_in") else ""))
        for c in cands:
            if c.get("blockers") and ph.purchase_cost(c) and c.get("page_campaign"):
                print(f"   BLOQ {c['type']:<7} {c.get('ref')} → {c['blockers'][-1][:150]}")
    print("\nResumen: con el mismo estado, la compra rentable de MAL-10 (28 P) se ejecuta en A, se bloquea en B porque dejaría"
          "\nmenos efectivo libre que w × meta (y no tiene salida respaldada), y se bloquea en C; en C además se retira la"
          "\npuja pasiva de 25 P y las ventas rentables de duplicados siguen permitidas con caducidad reducida.")


if __name__ == "__main__":
    main()
