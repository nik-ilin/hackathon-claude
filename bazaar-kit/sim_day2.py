"""Simulación local de Day 2 (sin red): dos tablones con comisiones distintas, El Duende sin comisión, trueques, una
oferta dirigida, una página a 2/3 y duplicados con competencia de precios. Usa el planificador y la selección REALES.

    python3 sim_day2.py
"""
import market_intel as mi
import coordinator as co
from test_market_intel import TEAM, A, ask, bid, snap, swap

ASSETS = [A(1, "LAT-01"), A(2, "LAT-02"),                   # La Latina 2/3: falta LAT-03 (completa la página)
          A(3, "MAL-01"), A(4, "MAL-01"),                   # duplicado: hay una puja en El Duende
          A(5, "MAL-02"), A(6, "MAL-02"),                   # duplicado: alguien lo quiere a cambio de LAT-03
          A(7, "MAL-03"), A(8, "MAL-03")]                   # duplicado: un rival lo vende a 30 P en El Rastro
BOARDS = [ask(100, "rastro", 70, "MAL-03", 30, maker="m_a"),              # rival en El Rastro (comprador paga +3 P)
          ask(101, "rastro", 71, "LAT-03", 16, maker="m_b"),              # LAT-03 a la venta con comisión
          bid(102, "rastro", "MAL-02", 6, maker="m_c"),                   # puja floja
          swap(103, "v02", 72, "LAT-03", "MAL-02", maker="m_d"),          # trueque excelente en El Duende
          bid(104, "v02", "MAL-01", 20, maker="m_e"),                     # puja que podemos llenar sin comisión
          ask(105, "v03", 73, "MAL-03", 29, maker="m_f")]                 # otro rival en Mercado Trece (1 %)
DIRECTED = [ask(106, "v02", 74, "LAT-03", 14, maker="t05", to=TEAM)]     # oferta dirigida a nosotros


def run_tick(s, actions=(), title=""):
    pl = mi.plan(s, mi.IntelConfig(), actions=actions, expiry_ratio=2.0)
    cands = [dict(o, module="mercado") for o in pl["opportunities"]]
    chosen = co.select(cands, {"actions": [], "class_tick": {}}, s["clock"]["tick"], max_posts=4)
    print(f"\n######## {title} (tick {s['clock']['tick']}) ########")
    print(mi.intel_report(pl, refs=["LAT-03", "MAL-01", "MAL-02", "MAL-03"]))
    print("\nSELECCIONADAS (1 aceptación por tick; publicaciones hasta 4):")
    for c in chosen:
        what = c.get("ref") or f"recibe {c.get('receive')} entrega {c.get('deliver')}"
        print(f"  {c['type']:<10} {c['kind']:<18} {what} · {c.get('price')} P en {c.get('venue')} · ΔU {c['du']} P · "
              f"esperado {c.get('expected_du')} P · {c.get('reason')}")
    return pl, chosen


if __name__ == "__main__":
    s1 = snap(ASSETS, BOARDS, mine=DIRECTED, cash=300, tick=100)
    pl, chosen = run_tick(s1, title="TICK 1")
    acc = next(c for c in chosen if c["type"] == "accept")
    assert acc["kind"] == "intercambio" and acc["venue"] == "v02", "debe aceptar el trueque que completa la página"
    lst = next(c for c in chosen if c["type"] == "list" and c["ref"] == "MAL-03")
    venues = mi.venues_from(s1)
    rival_cost = min(o["want"]["cash"] + venues[o["venue"]].fee(o["want"]["cash"], 1) for o in BOARDS
                     if o["id"] in (100, 105))
    assert lst["venue"] == "v02" and lst["price"] < rival_cost and lst["price"] >= lst["floor"], \
        "debe ser el vendedor más barato en coste total, sin bajar del suelo"
    assert not [c for c in chosen if c["type"] == "swap_list" and c.get("ref") == "LAT-03"], \
        "no se persigue LAT-03 por dos vías en el mismo tick"

    # tick 2: el trueque se liquidó (entregamos MAL-02 #6, recibimos LAT-03 #72); nuestra MAL-03 está publicada
    a2 = [a for a in ASSETS if a["id"] != 6] + [A(72, "LAT-03")]
    mine2 = [ask(200, "v02", 8, "MAL-03", lst["price"], maker=TEAM, created=100)]
    s2 = snap(a2, [o for o in BOARDS if o["id"] != 103], mine=mine2, cash=300, tick=101)
    pl2, chosen2 = run_tick(s2, title="TICK 2 · página completada, llenar la puja")
    acc2 = next(c for c in chosen2 if c["type"] == "accept")
    assert acc2["kind"] == "vender" and acc2["venue"] == "v02" and acc2["fee"] == 0, "debe llenar la puja de MAL-01"
    assert not [c for c in chosen2 if c.get("ref") == "LAT-03"], "LAT-03 ya está: no se compra otra copia"

    # tick 5: un rival rebaja MAL-03 a 7 P en El Duende tras dos reprecios nuestros -> no se entra en guerra
    s3 = snap(a2, [ask(300, "v02", 75, "MAL-03", 7, maker="m_g")], mine=[], cash=300, tick=105)
    war = [{"type": "cancel", "ref": "MAL-03", "reason": "reprecio: 31 → 24"},
           {"type": "cancel", "ref": "MAL-03", "reason": "reprecio: 24 → 15"}]
    pl3, chosen3 = run_tick(s3, actions=war, title="TICK 5 · guerra de precios")
    assert not [c for c in chosen3 if c["type"] == "list" and c.get("ref") == "MAL-03"], "no seguir la guerra de precios"
    why = [r["why"] for r in pl3["rejected"] if r.get("ref") == "MAL-03"]
    print("\nMAL-03 descartada:", why[:2])
    print("\nOK: elige v02 cuando conviene, rebaja con margen, acepta el trueque que completa la página, llena la puja y "
          "no entra en guerra de precios.")
