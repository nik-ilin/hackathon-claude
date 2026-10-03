"""Market profiler: turns intel/market.db into profiles of every actor and recommendations.

    python3 intel/profiler.py          # once
    python3 intel/profiler.py loop     # every 5 minutes (launchd: com.team15.profiler)

Outputs (regenerated each cycle, nothing is sent to the server):
  intel/REPORT.md              human-readable state of the market and of every actor
  intel/recommendations.json   machine-readable parameters for our strategies (the tuner loads it)
  tables profile_team / profile_dealer / price_index in market.db
"""
from __future__ import annotations

import collections
import json
import os
import sqlite3
import statistics as st
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "market.db")
REPORT = os.path.join(HERE, "REPORT.md")
RECS = os.path.join(HERE, "recommendations.json")
DEALERS = {"abuela", "chato"}           # extended automatically from dealer snapshots
US = "t15"


def med(xs):
    xs = [x for x in xs if x is not None]
    return round(st.median(xs), 2) if xs else None


def load(db):
    q = lambda sql, *a: db.execute(sql, a).fetchall()  # noqa: E731
    dealers = set(DEALERS)
    for (payload,) in q("SELECT payload FROM json_snapshots WHERE kind='dealers'"):
        for p in json.loads(payload).get("personas", []):
            dealers.add(p["id"])
    return q, dealers


# ── dealers & the teams that haggle with them ───────────────────────────
def haggles(q, dealers):
    """One record per persona thread: who, what, prices on both sides, outcome."""
    topics = {tid: json.loads(t) if t else None for tid, t in q("SELECT id, topic FROM threads")}
    rarity_of_asset = dict(q("SELECT asset_id, rarity FROM settlement_items WHERE rarity IS NOT NULL"))
    rarity_of_asset.update(q("SELECT asset_id, rarity FROM provenance WHERE rarity IS NOT NULL"))
    rarity_of_ref = dict(q("SELECT ref, rarity FROM provenance WHERE rarity IS NOT NULL"))
    msgs = collections.defaultdict(list)
    for tid, tick, team, with_, sender, price, final in q(
            "SELECT thread, tick, team, with_, sender, price, final FROM thread_messages "
            "WHERE kind='persona' ORDER BY thread, tick, id"):
        msgs[tid].append((tick, team, with_, sender, price, final))
    sett = collections.defaultdict(list)            # (team, dealer) -> [(tick, price)]
    for tick, persona, a, b, price in q("SELECT tick, persona, party_a, party_b, price FROM settlements WHERE persona IS NOT NULL"):
        team = a if a != persona else b
        sett[(team, persona)].append((tick, price))
    out = []
    for tid, ms in msgs.items():
        team = next((m[1] for m in ms if m[1]), None)
        dealer = next((m[2] for m in ms if m[2] in dealers), None)
        if not team or not dealer:
            continue
        topic = topics.get(tid) or {}
        mode = "sell" if "sell" in topic else ("buy" if topic else "?")
        if mode == "sell":
            ids = topic["sell"].get("assets", [])
            kind = rarity_of_asset.get(ids[0], "?") if len(ids) == 1 else f"lote{len(ids)}"
        elif mode == "buy":
            b = topic["buy"]
            kind = b.get("pack") or b.get("rarity") or rarity_of_ref.get(b.get("card"), "carta")
        else:
            kind = "?"
        d_prices = [m[4] for m in ms if m[3] == dealer and m[4] is not None]
        t_prices = [m[4] for m in ms if m[3] == team and m[4] is not None]
        final = next((m[4] for m in ms if m[3] == dealer and m[5]), None)
        last_tick = ms[-1][0]
        deal = next((p for t, p in sett.get((team, dealer), []) if last_tick - 3 <= t <= last_tick + 3), None)
        steps = [abs(b - a) for a, b in zip(t_prices, t_prices[1:])]
        out.append({"thread": tid, "team": team, "dealer": dealer, "mode": mode, "kind": kind,
                    "dealer_open": d_prices[0] if d_prices else None, "dealer_last": d_prices[-1] if d_prices else None,
                    "final": final, "team_open": t_prices[0] if t_prices else None, "rounds": len(t_prices),
                    "step": med(steps), "deal": deal, "tick": ms[0][0]})
    # Card refs missing from provenance: infer rarity from the dealer's opening price,
    # nearest to that dealer's median opening for known rarities (else "carta" mixes 30 and 86).
    opens = collections.defaultdict(list)
    for h in out:
        if h["mode"] == "buy" and h["kind"] in ("common", "uncommon", "rare") and h["dealer_open"]:
            opens[(h["dealer"], h["kind"])].append(h["dealer_open"])
    for h in out:
        if h["kind"] == "carta" and h["dealer_open"]:
            refs = {k: med(v) for (d, k), v in opens.items() if d == h["dealer"]}
            if refs:
                h["kind"] = min(refs, key=lambda k: abs(refs[k] - h["dealer_open"]))
    return out


def dealer_profiles(hs):
    """Per dealer × mode × kind: opening, finals, deal prices, concession per round."""
    g = collections.defaultdict(list)
    for h in hs:
        g[(h["dealer"], h["mode"], h["kind"])].append(h)
    prof = {}
    for (dealer, mode, kind), rows in g.items():
        conc = []
        for h in rows:
            if h["dealer_open"] is not None and h["dealer_last"] is not None and h["rounds"]:
                conc.append(abs(h["dealer_open"] - h["dealer_last"]) / h["rounds"])
        deals = [h["deal"] for h in rows if h["deal"] is not None]
        best = (min(deals) if mode == "buy" else max(deals)) if deals else None
        best_h = next((h for h in rows if h["deal"] == best), None)
        prof[f"{dealer}|{mode}|{kind}"] = {
            "threads": len(rows), "deals": len(deals),
            "open_median": med([h["dealer_open"] for h in rows]),
            "final_median": med([h["final"] for h in rows]),
            "deal_median": med(deals), "deal_best": best,
            "best_by": best_h and {"team": best_h["team"], "team_open": best_h["team_open"],
                                   "step": best_h["step"], "rounds": best_h["rounds"]},
            "dealer_concession_per_round": med(conc),
            "rounds_median": med([h["rounds"] for h in rows]),
        }
    return prof


# ── teams ───────────────────────────────────────────────────────────────
def team_profiles(q, hs, dealer_prof):
    teams = collections.defaultdict(lambda: collections.defaultdict(list))
    for h in hs:
        t = teams[h["team"]]
        t["haggles"].append(h)
    # capture: how far each deal sits inside the observed range for that dealer item
    for h in hs:
        p = dealer_prof.get(f"{h['dealer']}|{h['mode']}|{h['kind']}")
        if h["deal"] is None or not p or p["open_median"] is None or p["deal_best"] is None:
            continue
        rng = abs(p["open_median"] - p["deal_best"])
        if rng:
            cap = (p["open_median"] - h["deal"]) / rng if h["mode"] == "buy" else (h["deal"] - p["open_median"]) / rng
            teams[h["team"]]["capture"].append(max(0.0, min(1.0, cap)))
    # P2P settlements: what each team buys/sells, by set and rarity, and price vs market
    rar_price = collections.defaultdict(list)
    rows = q("""SELECT s.id, s.tick, s.venue, s.price, s.fee, s.n_items, i.ref, i.rarity, i.set_id, i.frm, i.too
                FROM settlements s JOIN settlement_items i ON i.settlement = s.id WHERE s.venue IS NOT NULL""")
    for sid, tick, venue, price, fee, n, ref, rarity, set_id, frm, too in rows:
        if n == 1 and rarity:
            rar_price[rarity].append(price)
    rmed = {r: st.median(v) for r, v in rar_price.items() if v}
    for sid, tick, venue, price, fee, n, ref, rarity, set_id, frm, too in rows:
        unit = price / max(1, n)
        rel = unit / rmed[rarity] if rarity in rmed and rmed[rarity] else None
        teams[too]["bought"].append((set_id, rarity, unit, rel, venue))
        teams[frm]["sold"].append((set_id, rarity, unit, rel, venue))
    # listings and bids (offer.listed): asks and wants
    for maker, ga, gc, wc, wt, venue in q("SELECT maker, give_assets, give_cash, want_cash, want_types, venue FROM offers WHERE venue IS NOT NULL"):
        ga, wt = json.loads(ga or "[]"), json.loads(wt or "[]")
        for a in ga:
            if isinstance(a, dict):
                teams[maker]["asks"].append((a.get("set"), a.get("rarity"), (wc or 0) / max(1, len(ga))))
        for t in wt:
            if t.startswith("card:"):
                teams[maker]["wants"].append((t[5:8], gc))
    # leaderboard trajectory
    lb = collections.defaultdict(list)
    for ts, tick, team, name, rank, score, neg, mkt, deals, level, album in q(
            "SELECT snap_ts, tick, team, name, rank, score, negotiating, market, deals, level, album_filled FROM leaderboard ORDER BY snap_ts"):
        lb[team].append({"ts": ts, "tick": tick, "name": name, "rank": rank, "score": score, "neg": neg,
                         "mkt": mkt, "deals": deals, "level": level, "album": album})
    venues = {}
    for (payload,) in q("SELECT payload FROM json_snapshots WHERE kind='venues' ORDER BY snap_ts DESC LIMIT 1"):
        for v in json.loads(payload):
            if v.get("status") == "closed":     # replaced starter stalls stay listed as closed
                continue
            venues.setdefault(v.get("owner"), []).append(v)

    # score deltas are only comparable inside a round: measure since the current round started
    rs = q("SELECT max(tick) FROM events WHERE type='round.started'")
    round_tick = rs[0][0] if rs and rs[0][0] is not None else 0
    prof = {}
    for team in set(teams) | set(lb):
        if not team or team in DEALERS or team.startswith("m"):     # skip dealers and pseudonyms
            continue
        t = teams[team]
        traj = lb.get(team, [])
        cur = traj[-1] if traj else {}
        hour_ago = next((x for x in traj if x["tick"] is not None and x["tick"] >= round_tick), cur)
        interest = collections.Counter()
        for s, r, u, rel, v in t["bought"]:
            interest[s] += 1
        for s, gc in t["wants"]:
            interest[s] += 1 + (gc or 0) / 30
        for s, r, u, rel, v in t["sold"]:
            interest[s] -= 0.5
        for s, r, p in t["asks"]:
            interest[s] -= 0.3
        hg = t["haggles"]
        prof[team] = {
            "name": cur.get("name"), "rank": cur.get("rank"), "score": cur.get("score"),
            "neg": cur.get("neg"), "mkt": cur.get("mkt"), "level": cur.get("level"), "album": cur.get("album"),
            "score_delta_1h": round((cur.get("score") or 0) - (hour_ago.get("score") or 0), 2) if cur else None,
            "dealer_threads": len(hg), "dealer_deals": sum(1 for h in hg if h["deal"] is not None),
            "dealer_capture": med(t["capture"]),
            "dealer_open_ratio": med([h["team_open"] / h["dealer_open"] for h in hg
                                      if h["team_open"] and h["dealer_open"]]),
            "dealer_step": med([h["step"] for h in hg]),
            "dealer_modes": dict(collections.Counter(h["mode"] for h in hg)),
            "p2p_bought": len(t["bought"]), "p2p_sold": len(t["sold"]),
            "buy_price_vs_market": med([x[3] for x in t["bought"]]),
            "sell_price_vs_market": med([x[3] for x in t["sold"]]),
            "listings": len(t["asks"]), "bids": len(t["wants"]),
            "set_interest": [s for s, _ in interest.most_common() if s and interest[s] > 0][:3],
            "set_disinterest": [s for s, v in sorted(interest.items(), key=lambda kv: kv[1]) if s and v < 0][:3],
            "venues": [f"{v['venue']} {v.get('fee_bps')}bps {v.get('rules', {}).get('mechanism')} trades={v.get('trades')}"
                       for v in venues.get(team, [])],
        }
    return prof, rmed


# ── recommendations for our strategies (the self-improvement feed) ───────
def recommendations(dealer_prof, team_prof, rmed):
    finals = {k: {"n": v["deals"] + (1 if v["final_median"] else 0),
                  "median": v["final_median"] if v["final_median"] is not None else v["deal_median"]}
              for k, v in dealer_prof.items() if (v["final_median"] or v["deal_median"]) is not None}
    steps = {k: v["best_by"]["step"] for k, v in dealer_prof.items() if v.get("best_by") and v["best_by"]["step"]}
    buyers_by_set = collections.defaultdict(list)
    for team, p in team_prof.items():
        if team == US:
            continue
        for s in p["set_interest"]:
            buyers_by_set[s].append(team)
    leaders = [t for t, p in sorted(team_prof.items(), key=lambda kv: kv[1]["rank"] or 99)[:3]]
    return {
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "dealer_finals": finals,                       # → strategy/dealers.PARAMS["FINALS"]
        "dealer_best_steps": steps,                    # step used by the team that got the best price
        "p2p_median_by_rarity": {r: round(v, 1) for r, v in rmed.items()},
        "buyers_by_set": dict(buyers_by_set),          # who to target when selling a set
        "leaders": leaders,                            # never trade on their venues
    }


def self_improve(dealer_prof, team_prof, q):
    """Close the loop: where we lose points vs the leader, the cheapest lever, and dealer params
    learned from the best observed deals (opening as a fraction of the dealer's opening, step, cap)."""
    me = team_prof.get(US, {})
    lead = min(team_prof.values(), key=lambda p: p["rank"] or 99) if team_prof else {}
    hist = q("SELECT tick, score, negotiating, market FROM leaderboard WHERE team=? ORDER BY snap_ts DESC LIMIT 13", US)
    trend = round(hist[0][1] - hist[-1][1], 2) if len(hist) > 1 else None   # ~last hour of snapshots
    stall = st.mode([p["mkt"] for p in team_prof.values() if p.get("mkt") is not None]) if team_prof else None
    params = {}
    for k, v in dealer_prof.items():
        b, op = v.get("best_by"), v.get("open_median")
        if b and b.get("team_open") and op:
            params[k] = {"open_frac": round(b["team_open"] / op, 2), "step": b["step"],
                         "cap": v["deal_best"], "target_final": v["final_median"]}
    levers = []
    if stall is not None and (me.get("mkt") or 0) <= stall:
        best_mm = max((p["mkt"] for p in team_prof.values() if p.get("mkt") is not None), default=stall)
        levers.append((round(best_mm - (me.get("mkt") or 0), 2),
                       f"market-making: conseguir tratos entre otros equipos en nuestro mercado (hoy "
                       f"{sum(int(x.rsplit('trades=', 1)[-1] or 0) for x in me.get('venues', []) if 'trades=' in x and x.rsplit('trades=', 1)[-1].isdigit())}; el mejor mercado saca {best_mm} vs puesto {stall})"))
    if me.get("dealer_capture") is not None and lead.get("dealer_capture"):
        gap = round((lead.get("neg") or 0) - (me.get("neg") or 0), 2)
        levers.append((gap, f"negociación: captura con dealers {me['dealer_capture']} vs {lead['dealer_capture']} del líder; "
                            f"tratos/hilos {me.get('dealer_deals')}/{me.get('dealer_threads')} → abrir más bajo y pasos cortos (ver dealer_params)"))
    levers.sort(reverse=True)
    # our live score breakdown (GET /api/me → score): which component actually moves
    parts = {}
    row = q("SELECT payload FROM me_snapshots ORDER BY snap_ts DESC LIMIT 1")
    if row:
        sc = json.loads(row[0][0]).get("score") or {}
        parts = {k: sc.get(k) for k in ("neg_points", "duel_points", "ladder_points", "mm_points",
                                        "bench_efficiency", "bench_points", "negotiating", "market")}
    return {"gap_to_first": round((lead.get("score") or 0) - (me.get("score") or 0), 2), "score_parts": parts,
            "our_trend_last_snapshots": trend, "levers": [t for _, t in levers], "dealer_params": params}


def render(dealer_prof, team_prof, rmed, recs, q):
    L = [f"# Inteligencia de mercado — {recs['generated']}", ""]
    si = recs.get("self_improve")
    if si:
        L += ["## Bucle de automejora (t15)", "",
              f"- Distancia al 1º: {si['gap_to_first']} · tendencia últimas instantáneas: {si['our_trend_last_snapshots']}"]
        if si.get("score_parts"):
            L.append("- Desglose propio (/api/me): " + " · ".join(f"{k} {v}" for k, v in si["score_parts"].items()))
        L += [f"- Palanca {i}: {t}" for i, t in enumerate(si["levers"], 1)]
        L += ["- Parámetros aprendidos del mejor trato por dealer/artículo en `recommendations.json` → `self_improve.dealer_params`", ""]
    # bad faith: words vs attached price (flag candidates) and bluffs (negotiation hint, never flag)
    try:
        import lies
        mism, bluffs = lies.scan(q)
        ours = [m for m in mism if m["team"] == US]
        L += ["## Mala fe de dealers", "",
              f"- Palabras ≠ precio de la oferta: {len(mism)} en total, {len(ours)} en nuestros hilos "
              "(candidatos a `POST /api/flags`; solo los nuestros, un flag erróneo resta)"]
        L += [f"  - msg {m['message_id']} {m['dealer']} t{m['tick']}: oferta {m['price']} vs palabras {m['words']} · «{m['text']}»"
              for m in ours[:5]]
        by = {}
        for b in bluffs:
            by.setdefault(b["dealer"], []).append(b)
        L += [f"- Faroles «final» sin `final: true` que luego se mueven: " +
              ("; ".join(f"{d} ×{len(v)} (p. ej. {v[-1]['said']}→{v[-1]['then']})" for d, v in by.items()) or "ninguno")
              + " → seguir regateando", ""]
    except Exception as e:  # analysis only: never break the report
        L += ["## Mala fe de dealers", "", f"- error: {e!r}", ""]
    # announcements (new levels, venue ads) and offers that touch us (directed to t15 or listed on our venue)
    try:
        # game-wide news and new features first (rare, high signal), then the noisy venue ads
        ev = q("SELECT tick, type, actor, payload FROM events WHERE type IN ('level.announced','level.activated',"
               "'news.posted','persona.updated') ORDER BY id DESC LIMIT 6")
        ev += q("SELECT tick, type, actor, payload FROM events WHERE type='taller.crafted' ORDER BY id DESC LIMIT 3")
        ev += q("SELECT tick, type, actor, payload FROM events WHERE type='venue.announcement' ORDER BY id DESC LIMIT 5")
        offs = q("SELECT tick, payload FROM events WHERE type='offer.listed' AND "
                 "(payload LIKE '%\"to\": \"t15\"%' OR payload LIKE '%\"venue\": \"v15\"%') ORDER BY id DESC LIMIT 6")
        if ev or offs:
            L += ["## Anuncios y ofertas que nos tocan", ""]
            for tick, typ, actor, p in ev:
                d = json.loads(p)
                if typ in ("level.announced", "level.activated"):
                    txt = (f"nivel {'ACTIVO' if typ == 'level.activated' else 'anunciado'} «{d.get('name')}» "
                           f"({d.get('kind')}): {(d.get('how') or d.get('teaser') or '')[:160]}")
                elif typ == "news.posted":  # Boletín = oficial; Radio = a veces cierto; Tablón = rumor
                    txt = f"NOTICIA [{d.get('source_name')}] {d.get('headline')} — {(d.get('body') or '')[:100]}"
                elif typ == "taller.crafted":
                    txt = f"taller: {(d.get('text') or '')[:120]}"
                elif typ == "persona.updated":
                    txt = f"dealer {d.get('name')} cambia a versión {d.get('version')}: recalibrar su escalera"
                else:
                    txt = f"{actor} «{d.get('name')}»: {(d.get('text') or '')[:120]}"

                L.append(f"- t{tick} {txt}")
            for tick, p in offs:
                o = json.loads(p)["offer"]
                side = lambda s: ([a["ref"] for a in s.get("assets", [])] + list(s.get("types", [])) +
                                  ([f"{s['cash']}P"] if s.get("cash") else []))
                L.append(f"- t{tick} oferta {o['id']} {o['maker']}→{o.get('to') or 'todos'} en {o['venue']}: "
                         f"da {side(o['give'])} pide {side(o['want'])} (caduca t{o.get('expires_tick')})")
            L.append("")
    except Exception as e:  # analysis only: never break the report
        L += ["## Anuncios y ofertas que nos tocan", "", f"- error: {e!r}", ""]
    # venue changes over the last ~hour of snapshots (fee cuts, new venues, first trades)
    vs = q("SELECT tick, payload FROM json_snapshots WHERE kind='venues' ORDER BY snap_ts DESC LIMIT 7")
    if len(vs) > 1:
        def vmap(p):
            return {v["venue"]: v for v in json.loads(p)}
        new, old = vmap(vs[0][1]), vmap(vs[-1][1])
        ch = []
        for vid, v in new.items():
            o = old.get(vid)
            if o is None:
                ch.append(f"{vid} nuevo de {v['owner']} ({v['fee_bps']}bps {v['rules'].get('mechanism')})")
            else:
                if o["fee_bps"] != v["fee_bps"]:
                    ch.append(f"{vid} ({v['owner']}) comisión {o['fee_bps']}→{v['fee_bps']}bps")
                if (v.get("trades") or 0) > (o.get("trades") or 0):
                    ch.append(f"{vid} ({v['owner']}) tratos {o.get('trades') or 0}→{v['trades']}")
        L += [f"## Cambios en mercados (ticks {vs[-1][0]}→{vs[0][0]})", ""] + [f"- {c}" for c in ch or ["sin cambios"]] + [""]
    clock = q("SELECT payload FROM json_snapshots WHERE kind='clock' ORDER BY snap_ts DESC LIMIT 1")
    if clock:
        L.append(f"Ronda: {json.loads(clock[0][0]).get('round_name')}")
    L += ["", "## Clasificación y perfil de equipos", "",
          "| # | Equipo | Score | Δronda | Neg | MM | Nivel | Álbum | Dealer: tratos/hilos | Captura rango | Apertura vs dealer | Paso | P2P compra/venta | Compra vs mercado | Venta vs mercado | Le interesa | Le sobra | Mercado propio |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for team, p in sorted(team_prof.items(), key=lambda kv: kv[1]["rank"] or 99):
        L.append(f"| {p['rank']} | {p['name'] or team} ({team}) | {p['score']} | {p['score_delta_1h']} | {p['neg']} | {p['mkt']} | "
                 f"{p['level']} | {p['album']} | {p['dealer_deals']}/{p['dealer_threads']} | {p['dealer_capture']} | "
                 f"{p['dealer_open_ratio']} | {p['dealer_step']} | {p['p2p_bought']}/{p['p2p_sold']} | "
                 f"{p['buy_price_vs_market']} | {p['sell_price_vs_market']} | {', '.join(p['set_interest'])} | "
                 f"{', '.join(p['set_disinterest'])} | {'; '.join(p['venues'])} |")
    L += ["", "## Dealers: cómo ceden", "",
          "| Dealer · modo · artículo | Hilos | Tratos | Apertura | Final (mediana) | Trato mediano | Mejor trato | Lo consiguió | Cesión/ronda | Rondas |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for k, v in sorted(dealer_prof.items()):
        b = v["best_by"]
        who = f"{b['team']} (abre {b['team_open']}, paso {b['step']}, {b['rounds']} r.)" if b else ""
        L.append(f"| {k.replace('|', ' · ')} | {v['threads']} | {v['deals']} | {v['open_median']} | {v['final_median']} | "
                 f"{v['deal_median']} | {v['deal_best']} | {who} | {v['dealer_concession_per_round']} | {v['rounds_median']} |")
    # Market Test: who beats the free stall (stall = the modal market score)
    mm = [(p["mkt"], t, p["venues"]) for t, p in team_prof.items() if p.get("mkt") is not None]
    if mm:
        stall = st.mode([m for m, _, _ in mm])
        L += ["", f"## Market Test — puesto gratuito = {stall}", ""]
        for m, t, v in sorted(mm, reverse=True):
            if m != stall:
                tag = "SUPERA al puesto" if m > stall else "por DEBAJO del puesto"
                L.append(f"- {t}: {m} ({tag}) · {'; '.join(v) or 'sin mercado propio'}")
        for (tick, payload) in q("SELECT tick, payload FROM events WHERE type LIKE 'bench.%' ORDER BY tick DESC LIMIT 3"):
            L.append(f"- evento tick {tick}: {payload[:160]}")
    # what is being done with OUR key this round (whatever process does it)
    rs = q("SELECT max(tick) FROM events WHERE type='round.started'")
    rt = rs[0][0] if rs and rs[0][0] is not None else 0
    th = q("SELECT with_, topic, count(*) FROM threads WHERE team=? AND opened_tick>=? GROUP BY 1,2 ORDER BY 3 DESC", US, rt)
    dl = q("SELECT count(*) FROM settlements WHERE (party_a=? OR party_b=?) AND persona IS NOT NULL AND tick>=?", US, US, rt)[0][0]
    ven = q("SELECT venue, count(*) FROM offers WHERE maker=? AND tick>=? GROUP BY 1", US, rt)
    leaders = set(recs["leaders"])
    lv = {v.get("venue") for t, p in team_prof.items() if t in leaders for v in [] }
    leader_venues = {x.split()[0] for t in leaders for x in team_prof.get(t, {}).get("venues", [])}
    L += ["", f"## Actividad con nuestra clave en esta ronda (desde tick {rt})", "",
          f"- Hilos con dealers: {sum(n for _, _, n in th)} · tratos cerrados con dealers: {dl}"]
    L += [f"  - {w}: {t} ×{n}" for w, t, n in th[:8]]
    L += [f"- Ofertas publicadas en {v}: {n}" + ("  ⚠️ mercado de un LÍDER (le suma market-making)" if v in leader_venues else "")
          for v, n in ven]
    L += ["", "## Precios P2P liquidados (mediana por carta suelta)", ""]
    L += [f"- {r}: {round(v, 1)} P" for r, v in sorted(rmed.items())]
    L += ["", "## A quién vender cada set", ""]
    L += [f"- {s}: {', '.join(t)}" for s, t in sorted(recs["buyers_by_set"].items())]
    L += ["", f"Líderes (no operar en sus mercados): {', '.join(recs['leaders'])}", ""]
    return "\n".join(L)


def run_once() -> None:
    db = sqlite3.connect(DB, timeout=30)  # collector writes concurrently
    q, dealers = load(db)
    hs = haggles(q, dealers)
    dprof = dealer_profiles(hs)
    tprof, rmed = team_profiles(q, hs, dprof)
    recs = recommendations(dprof, tprof, rmed)
    recs["self_improve"] = self_improve(dprof, tprof, q)
    db.executescript("""
      CREATE TABLE IF NOT EXISTS profile_team (ts REAL, team TEXT, payload TEXT);
      CREATE TABLE IF NOT EXISTS profile_dealer (ts REAL, key TEXT, payload TEXT);""")
    now = time.time()
    db.executemany("INSERT INTO profile_team VALUES (?,?,?)", [(now, t, json.dumps(p)) for t, p in tprof.items()])
    db.executemany("INSERT INTO profile_dealer VALUES (?,?,?)", [(now, k, json.dumps(p)) for k, p in dprof.items()])
    db.commit()
    for path, text in ((REPORT, render(dprof, tprof, rmed, recs, q)), (RECS, json.dumps(recs, indent=1))):
        with open(path + ".tmp", "w") as f:
            f.write(text)
        os.replace(path + ".tmp", path)
    print(time.strftime("%H:%M:%S"), f"profiled {len(tprof)} teams, {len(dprof)} dealer items, {len(hs)} haggles", flush=True)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "loop":
        while True:
            try:
                run_once()
            except Exception as e:  # never die: keep profiling on the next cycle
                print("profiler error:", repr(e), flush=True)
            time.sleep(300)
    else:
        run_once()
