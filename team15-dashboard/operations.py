"""Estado operativo del domingo para humanos y agentes; sólo transforma lecturas existentes."""
from __future__ import annotations

from collections import Counter


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _cash_commitments(offers: list[dict], team: str = "t15") -> int:
    total = 0
    for offer in offers:
        if not isinstance(offer, dict) or offer.get("status") != "open":
            continue
        # /api/me/offers also includes incoming offers. Cash offered by a
        # counterparty is reserved on their side, not ours.
        if offer.get("maker") != team:
            continue
        cash = _number((offer.get("give") or {}).get("cash"))
        if cash is not None:
            total += max(0, int(cash))
    return total


def duel_watch(duels: list[dict], tick: int | None) -> dict:
    """Cola de cierres, sin atribuir puntos ni aceptar con datos de utilidad ausentes."""
    if tick is None:
        return {"status": "clock_unavailable", "live": 0, "rows": []}
    live = [d for d in duels if d.get("status") == "live" and _number(d.get("deadline_tick")) is not None]
    counts = Counter(d["deadline_tick"] for d in live)
    rows = []
    for d in sorted(live, key=lambda x: (x["deadline_tick"], str(x.get("duel")))):
        role, limit = d.get("role"), _number(d.get("your_limit"))
        offer = d.get("rival_offer") or {}
        price = _number(offer.get("price"))
        left = max(0, int(d["deadline_tick"] - tick))
        price_margin = ((limit - price) if role == "buyer" else (price - limit)) if price is not None and limit is not None and role in ("buyer", "seller") else None
        days_issue = "days" in (d.get("issues") or [])
        days_known = not days_issue
        total_margin = price_margin
        if days_issue:
            try:
                import duels as policy
                day = offer.get("days")
                days_known = policy.days_known(d) and isinstance(day, int) and 0 <= day <= 10
                total_margin = policy.margin(d, price, day) if days_known and price_margin is not None else None
            except (KeyError, TypeError, ValueError):
                total_margin = None
        safe = price_margin is not None and price_margin >= 0 and total_margin is not None and total_margin > 0
        if left == 0:
            state, action = "expired", "Verificar resultado"
        elif not safe and price is None:
            state, action = "silent", "Preparar oferta dentro del límite"
        elif not safe:
            state, action = "unsafe", "No aceptar; revisar precio y día"
        elif left <= counts[d["deadline_tick"]] + 1:
            state, action = "close_now", "Cerrar por cola de aceptaciones"
        else:
            state, action = "safe", "Oferta rentable; seguir tendencia y plazo"
        rows.append({"duel": d.get("duel"), "role": role, "deadline_tick": d["deadline_tick"],
                     "ticks_left": left, "same_deadline": counts[d["deadline_tick"]],
                     "rival_price": price, "rival_day": offer.get("days"),
                     "price_margin": price_margin, "total_margin": total_margin,
                     "days_known": days_known, "safe_to_accept": safe,
                     "state": state, "action": action,
                     "source": "/api/duels", "observed_tick": tick})
    return {"status": "ok", "live": len(rows), "safe": sum(r["safe_to_accept"] for r in rows),
            "urgent": sum(r["state"] == "close_now" for r in rows), "rows": rows,
            "accepts_per_tick": 1, "note": "La oferta puede cambiar; releer antes de aceptar."}


def capital_watch(cash, offers: list[dict], reserve: int = 100, team: str = "t15") -> dict:
    cash = _number(cash)
    if cash is None:
        return {"status": "private_data_unavailable", "cash": None}
    committed = _cash_commitments(offers, team)
    free = max(0, cash - committed)
    venue_need = 250 + 20 + 20 + max(0, reserve)
    return {"status": "ok", "cash": cash, "open_bid_commitments": committed,
            "uncommitted_cash": free, "operating_reserve": max(0, reserve),
            "spendable_after_reserve": max(0, free - max(0, reserve)),
            "board_preflight_need": venue_need,
            "board_cash_ready": cash >= venue_need + committed,
            "note": "El umbral de board incluye fianza 250, apertura 20, colchón 20 y reserva; no sustituye al preflight del broker."}


def market_watch(venues: list[dict], team: str = "t15") -> dict:
    own = next((v for v in venues if v.get("owner") == team), None)
    if not own:
        return {"status": "venue_unavailable", "venue": None}
    mechanism = (own.get("rules") or {}).get("mechanism")
    return {"status": own.get("status"), "venue": own.get("venue"),
            "mechanism": mechanism, "trades": own.get("trades"),
            "broker_required": mechanism == "board",
            "broker_health_verified": False,
            "note": "La API pública del venue no demuestra que el broker esté vivo; comprobar latido local y eventos bench."}


def decision_queue(duel: dict, capital: dict, market: dict, feed: dict, verified: bool) -> list[dict]:
    items = []
    if duel.get("status") == "unavailable":
        items.append({"priority": "critical", "topic": "duels", "action": "Restaurar lectura privada de duelos", "evidence": duel.get("error")})
    for row in duel.get("rows") or []:
        if row["state"] == "close_now":
            items.append({"priority": "critical", "topic": "duels", "action": f"Revisar y cerrar duelo {row['duel']}",
                          "evidence": f"{row['ticks_left']} ticks; {row['same_deadline']} duelos con ese plazo; margen {row['total_margin']} P"})
    if feed.get("status") != "fresh":
        delay = feed.get("ticks_behind")
        items.append({"priority": "high", "topic": "data", "action": "Actualizar recolector del feed",
                      "evidence": (f"estado {feed.get('status')}; retraso {delay} ticks"
                                   if delay is not None else f"estado {feed.get('status')}; sin eventos locales")})
    if not verified:
        items.append({"priority": "high", "topic": "valuation", "action": "Reconciliar valoración privada",
                      "evidence": "Las propuestas numéricas no están verificadas con /api/me"})
    if market.get("broker_required") and not market.get("broker_health_verified"):
        items.append({"priority": "high", "topic": "market", "action": "Confirmar latido del broker local",
                      "evidence": "Venue board: la API pública no acredita que el proceso esté vivo"})
    if capital.get("status") == "ok" and not capital.get("board_cash_ready") and market.get("mechanism") == "auto":
        items.append({"priority": "medium", "topic": "capital", "action": "Conservar capital operativo; board no supera preflight de caja",
                      "evidence": f"libre {capital['uncommitted_cash']} P; umbral {capital['board_preflight_need']} P"})
    return items


def build(*, duels: list[dict] | None, duel_error: str | None, tick: int | None,
          cash, offers: list[dict], reserve: int, venues: list[dict],
          feed_health: dict, verified: bool, team: str = "t15") -> dict:
    duel = duel_watch(duels or [], tick) if duels is not None else {"status": "unavailable", "error": duel_error or "Sin clave o lectura", "live": None, "rows": []}
    capital = capital_watch(cash, offers, reserve, team)
    market = market_watch(venues, team)
    return {"duels": duel, "capital": capital, "market": market,
            "queue": decision_queue(duel, capital, market, feed_health, verified),
            "source_tick": tick, "read_only": True}
