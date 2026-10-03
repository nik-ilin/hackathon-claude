"""Read-only, team-specific trade ranking. Monetary ΔU is not leaderboard points."""
from __future__ import annotations

import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

KIT = Path(__file__).resolve().parents[1] / "bazaar-kit"
if str(KIT) not in sys.path:
    sys.path.insert(0, str(KIT))

import trading as tr


def offer_team(offer: dict, listing_teams: dict[int, str]) -> str | None:
    """Books use aliases; only a matching public feed event proves the real team."""
    maker = offer.get("maker")
    if isinstance(maker, str) and len(maker) == 3 and maker.startswith("t") and maker[1:].isdigit():
        return maker
    return listing_teams.get(offer.get("id"))


def _cash_and_assets(me: dict, offers: list[dict]):
    assets = [a for a in me.get("assets", []) if a.get("kind") == "card"]
    locked = tr.resources(offers, me["id"], []).locked_assets
    return assets, locked


def build_rank(me: dict, catalog: dict, clock: dict, venues: list[dict], boards: dict[str, list[dict]],
               own_offers: list[dict], rivals: dict, listing_teams: dict[int, str],
               *, reserve: int = 100, margin: float = 2.0,
               market_refs: dict | None = None) -> dict:
    """Rank immediately acceptable offers and conditional team proposals.

    Offers are valued with the repo's verified private-value model. Proposals
    show our conditional surplus but never claim the rival will accept.
    """
    result = {"team": me.get("id"), "tick": clock.get("tick"), "verified": False,
              "cash": me.get("cash"), "collection_value": me.get("collection_value"),
              "score": me.get("score") or {}, "inventory": [], "needs": [],
              "teams": [], "trades": [], "warnings": []}
    rivals_by_team = {r["team"]: r for r in rivals.get("teams", []) if r.get("team") != "t15"}
    for team, rv in sorted(rivals_by_team.items()):
        result["teams"].append({"team": team, "observed_held": rv.get("held") or [],
                                "declared_wants": rv.get("sought") or [],
                                "offered": rv.get("duplicates_offered") or [],
                                "sample": rv.get("sample") or 0})
    if not me.get("id") or not catalog.get("sets"):
        result["warnings"].append("Faltan datos privados del equipo o catálogo; no se calculan operaciones.")
        return result
    val = tr.Valuation(catalog, me.get("affinity") or {})
    assets, locked = _cash_and_assets(me, own_offers)
    resources = tr.resources(own_offers, me["id"], [])
    counts = tr.counts_of(assets)
    other_value = sum(float(a.get("your_value") or 0) for a in me.get("assets", []) if a.get("kind") != "card")
    server_value = me.get("collection_value")
    verified, model = val.calibrate(counts, None if server_value is None else float(server_value) - other_value)
    result["verified"] = verified
    result["model_value"] = round(model + other_value, 2)
    if not verified:
        result["warnings"].append("La valoración local no cuadra con /api/me; se oculta el ranking numérico.")
    by_ref = defaultdict(list)
    for a in assets:
        by_ref[a["ref"]].append(a)
    surplus = {}
    for ref, copies in sorted(by_ref.items()):
        free = [a for a in sorted(copies, key=lambda a: a["id"], reverse=True) if a["id"] not in locked]
        committed = len(copies) - len(free)
        can_give = max(0, min(len(free), len(copies) - 1 - committed))
        loss = None
        if can_give and verified:
            loss = -val.delta(counts, Counter(), Counter({ref: 1}))[0]
            surplus[ref] = {"asset": free[0]["id"], "loss": loss, "copies": len(copies)}
        result["inventory"].append({"ref": ref, "copies": len(copies), "free_surplus": can_give,
                                    "loss": loss, "asset": free[0]["id"] if can_give else None})
    free_to_give = {item["ref"]: item["free_surplus"] for item in result["inventory"]}
    for ref, card in val.cards.items():
        if not card.get("released") or card.get("hidden") or val.unit(ref) is None:
            continue
        if counts.get(ref, 0) > 0:
            continue
        gain, notes = val.delta(counts, Counter({ref: 1}), Counter())
        result["needs"].append({"ref": ref, "gain": gain if verified else None,
                                "page": next((n for n in notes if "completa la página" in n), None)})
    result["needs"].sort(key=lambda r: -(r["gain"] or 0))

    venue_by_id = {v.get("venue"): v for v in venues if v.get("status") == "open"}
    own_venue = me.get("venue")
    own_venue = own_venue.get("venue") if isinstance(own_venue, dict) else own_venue
    if not verified:
        return result
    tick = int(clock.get("tick") or 0)
    seen_offers = set()
    for venue_id, offers in boards.items():
        venue = venue_by_id.get(venue_id)
        if not venue or venue_id == own_venue:
            continue
        for o in offers:
            if o.get("id") in seen_offers:
                continue
            seen_offers.add(o.get("id"))
            team = offer_team(o, listing_teams)
            if team is None or team == me["id"]:
                continue  # never attribute a pseudonym to a guessed team
            p, reason = tr.parse_offer(o, team=me["id"], tick=tick, own_venue=own_venue,
                                       my_assets=assets, locked=locked)
            if p is None:
                continue
            try:
                ev = tr.evaluate(p, val, counts, venue)
            except (ValueError, TypeError, KeyError):
                continue
            if (ev.blockers or ev.du < margin or
                any(free_to_give.get(ref, 0) < n for ref, n in p.deliver.items()) or
                p.cash_out + ev.fee > int(me.get("cash") or 0) - reserve - resources.reserved_cash):
                continue
            give = [f"{ref} ×{n}" for ref, n in p.deliver.items()]
            receive = [f"{ref} ×{n}" for ref, n in p.receive.items()]
            direction = "Vender" if p.deliver and not p.receive else "Comprar" if p.receive and not p.deliver else "Canjear"
            result["trades"].append({"kind": "live", "team": team, "action": direction,
                "give": give, "receive": receive, "price": p.price, "cash_in": p.cash_in,
                "cash_out": p.cash_out, "fee": ev.fee, "delta_value": ev.dv,
                "surplus": ev.du, "venue": venue_id, "offer_id": p.offer_id,
                "expires": p.expires_tick, "confidence": "Oferta activa",
                "why": f"Oferta #{p.offer_id} visible en {venue_id}; ΔU = efectivo neto {ev.cash:+.1f} P + colección {ev.dv:+.1f} P.",
                "notes": ev.notes})

    # A declared want identifies a counterparty, not a guaranteed sale.
    active_sale_keys = {(t["team"], ref) for t in result["trades"] if t["action"] == "Vender" for ref in
                        (p.split(" ×")[0] for p in t["give"])}
    for team, rv in rivals_by_team.items():
        for ref in rv.get("sought") or []:
            if ref not in surplus or (team, ref) in active_sale_keys:
                continue
            loss = surplus[ref]["loss"]
            market = None
            # A historical bid is only a price anchor, never treated as current.
            observed = (rv.get("best_bids") or {}).get(ref)
            if isinstance(observed, (int, float)) and observed > 0:
                market = int(observed)
            floor = math.ceil(loss + margin)
            ref_market = (market_refs or {}).get(ref) or {}
            fair = ref_market.get("fair")
            fair = math.ceil(fair) if isinstance(fair, (int, float)) and fair > 0 else None
            price = max(floor, market or floor, fair or floor)
            result["trades"].append({"kind": "proposal", "team": team, "action": "Proponer venta",
                "give": [ref], "receive": [], "price": price, "cash_in": price, "cash_out": 0,
                "fee": 0, "delta_value": -loss, "surplus": round(price - loss, 2),
                "venue": None, "offer_id": None, "expires": None,
                "confidence": "Demanda declarada; aceptación incierta",
                "why": f"{team} pidió {ref} en el feed. Suelo nuestro {floor} P = pérdida {loss:.1f} P + margen {margin:.1f} P."
                       + (f" Referencia de mercado {fair} P ({ref_market.get('confidence','sin confianza')})." if fair else "")
                       + (f" Última puja observada {market} P; puede haber caducado." if market else " Sin puja vigente verificada."),
                "notes": []})
    missing = {item["ref"] for item in result["needs"]}
    for team, rv in rivals_by_team.items():
        give_refs = [ref for ref in rv.get("sought") or [] if ref in surplus]
        receive_refs = [ref for ref in rv.get("duplicates_offered") or [] if ref in missing]
        candidates = []
        for give in give_refs:
            for receive in receive_refs:
                dv, notes = val.delta(counts, Counter({receive: 1}), Counter({give: 1}))
                if dv < margin or any("master" in note for note in notes):
                    continue
                candidates.append({"kind": "proposal", "team": team, "action": "Proponer canje",
                    "give": [give], "receive": [receive], "price": 0, "cash_in": 0, "cash_out": 0,
                    "fee": 0, "delta_value": dv, "surplus": dv, "venue": None,
                    "offer_id": None, "expires": None,
                    "confidence": "Intereses observados; aceptación incierta",
                    "why": f"{team} pidió {give} y ofreció {receive}. Para t15, recibir {receive} y dar {give} cambia la colección {dv:+.1f} P."
                           " Sin efectivo; la comisión propia sería cero si el rival acepta una propuesta nuestra.",
                    "notes": notes})
        result["trades"].extend(sorted(candidates, key=lambda c: -c["surplus"])[:3])
    result["trades"].sort(key=lambda t: (t["kind"] != "live", -t["surplus"], t["team"], t["price"]))
    return result
