"""Authorized Team 5 listings on v10 and feed-confirmed commission accounting."""
from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

APPROVER = "t05"
VENUE = "v10"
THREAD_ID = 1179
MESSAGE_ID = 7728
DEADLINE = "18:30"
TZ = ZoneInfo("Europe/Madrid")
LISTINGS = {
    "LAV-01": {"buyer": "t09", "price": 9},
    "MAL-07": {"buyer": "t02", "price": 14},
    "RET-01": {"buyer": "t09", "price": 9},
}


def _deadline(now: datetime | None = None) -> datetime:
    now = now or datetime.now(TZ)
    now = now.astimezone(TZ) if now.tzinfo else now.replace(tzinfo=TZ)
    hour, minute = map(int, DEADLINE.split(":"))
    result = datetime.combine(now.date(), time(hour, minute), TZ)
    if result <= now:
        raise ValueError("aprobación vencida; no se extiende a otro día")
    return result


def sync_approval(state: dict, thread: dict, team: str, now: datetime | None = None) -> list[str]:
    """Import only message 7728 from t05 in official team thread 1179."""
    if not thread or str(thread.get("id")) != str(THREAD_ID) or thread.get("kind") != "team":
        return []
    parties = {str(thread.get(k, "")).lower() for k in ("team", "with")}
    if APPROVER not in parties or str(team).lower() not in parties:
        return []
    messages = [m for m in thread.get("messages", []) if str(m.get("id")) == str(MESSAGE_ID)]
    state.setdefault("approvals", {})
    state.setdefault("rejected_messages", {})
    changed = []
    key = f"{THREAD_ID}:{MESSAGE_ID}"
    for message in messages:
        if str(message.get("sender", "")).lower() != APPROVER:
            continue
        body = str(message.get("text") or "")
        prior = [a for a in state["approvals"].values() if a.get("source_key") == key]
        if prior:
            if any(a.get("body") != body for a in prior):
                for approval in prior:
                    approval.update(status="tampered", error="el mensaje aprobado cambió")
                changed.append(key)
            continue
        try:
            expires = _deadline(now)
            if message.get("offer") is not None:
                raise ValueError("la autorización debe ser un mensaje, sin oferta estructurada")
        except ValueError as exc:
            state["rejected_messages"][key] = {"body": body, "reason": str(exc)}
            continue
        for card, terms in LISTINGS.items():
            sale_key = f"{key}:{card}"
            state["approvals"][sale_key] = {
                **terms, "card": card, "key": sale_key, "source_key": key,
                "thread_id": THREAD_ID, "message_id": MESSAGE_ID, "author_team": APPROVER,
                "body": body, "expires_at": expires.isoformat(), "status": "approved",
                "venue": VENUE, "commission_per_sale": 1,
            }
            changed.append(sale_key)
    return changed


import re

STRICT_FIELDS = ("card", "buyer", "price", "venue", "date", "valid_until", "commission", "commission_payer")
_TEAM = re.compile(r"^t\d+$")


def parse_structured_approval(text: str, now: datetime | None = None) -> tuple[dict | None, str]:
    """Aprobación del formato estricto de UNA línea, con TODOS los campos y sin ambigüedad sobre la comisión:

        APPROVE card=MAL-07 buyer=t02 price=14 venue=v10 date=2026-10-03 valid_until=18:30 commission=1 commission_payer=t05

    `commission` es lo que Team 5 abona a Team 15 por venta liquidada (puede ser 0) y `commission_payer` quién la abona.
    Falta un campo, un valor raro, otra fecha distinta de hoy o una hora ya pasada ⇒ se rechaza con el motivo. La
    comisión por cobrar NUNCA se trata como efectivo."""
    line = str(text or "").strip()
    if "\n" in line or not line.startswith("APPROVE "):
        return None, "debe ser una sola línea que empiece por APPROVE"
    kv = {}
    for part in line[len("APPROVE "):].split():
        if "=" not in part:
            return None, f"campo mal formado: {part!r}"
        k, v = part.split("=", 1)
        if k in kv:
            return None, f"campo repetido: {k}"
        kv[k] = v
    missing = [f for f in STRICT_FIELDS if f not in kv]
    extra = [k for k in kv if k not in STRICT_FIELDS]
    if missing or extra:
        return None, f"campos obligatorios ausentes {missing} o desconocidos {extra}"
    if not re.fullmatch(r"[A-Z]{3}-\d{2}", kv["card"]):
        return None, "card no válida"
    if not _TEAM.match(kv["buyer"]) or not _TEAM.match(kv["commission_payer"]):
        return None, "buyer y commission_payer deben ser ids de equipo (tNN)"
    if not re.fullmatch(r"v\d+|rastro", kv["venue"]):
        return None, "venue no válido"
    try:
        price, commission = int(kv["price"]), int(kv["commission"])
        day = datetime.strptime(kv["date"], "%Y-%m-%d").date()
        hh, mm = map(int, kv["valid_until"].split(":"))
        deadline = datetime.combine(day, time(hh, mm), TZ)
    except ValueError:
        return None, "price/commission enteros, date AAAA-MM-DD y valid_until HH:MM"
    if price < 1 or commission < 0:
        return None, "price ≥ 1 y commission ≥ 0"
    now = now or datetime.now(TZ)
    now = now.astimezone(TZ) if now.tzinfo else now.replace(tzinfo=TZ)
    if deadline <= now:
        return None, "aprobación vencida"
    if day != now.date():
        return None, "la fecha debe ser hoy (no se extiende a otro día)"
    return {"card": kv["card"], "buyer": kv["buyer"], "price": price, "venue": kv["venue"],
            "commission_per_sale": commission, "commission_payer": kv["commission_payer"],
            "expires_at": deadline.isoformat()}, ""


def sync_structured(state: dict, thread: dict, team: str, now: datetime | None = None) -> list[str]:
    """Importa aprobaciones estrictas de t05 de un hilo oficial de EQUIPO (t05↔nosotros). Cada mensaje queda ligado a
    su id inmutable; si su cuerpo cambia después se marca `tampered`. Los mensajes ambiguos se registran y rechazan."""
    if not thread or thread.get("kind") != "team":
        return []
    parties = {str(thread.get(k, "")).lower() for k in ("team", "with")}
    if APPROVER not in parties or str(team).lower() not in parties:
        return []
    state.setdefault("approvals", {})
    state.setdefault("rejected_messages", {})
    changed = []
    for message in thread.get("messages", []):
        if str(message.get("sender", "")).lower() != APPROVER or not str(message.get("text") or "").startswith("APPROVE"):
            continue
        key = f"{thread.get('id')}:{message.get('id')}"
        sale_key = f"{key}:strict"
        body = str(message.get("text") or "")
        prior = state["approvals"].get(sale_key)
        if prior:
            if prior.get("body") != body:
                prior.update(status="tampered", error="el mensaje aprobado cambió")
                changed.append(sale_key)
            continue
        if message.get("offer") is not None:
            state["rejected_messages"][key] = {"body": body, "reason": "debe ser un mensaje, sin oferta estructurada"}
            continue
        terms, why = parse_structured_approval(body, now)
        if terms is None:
            state["rejected_messages"][key] = {"body": body, "reason": why}
            continue
        state["approvals"][sale_key] = {**terms, "key": sale_key, "source_key": key, "thread_id": thread.get("id"),
                                        "message_id": message.get("id"), "author_team": APPROVER, "body": body,
                                        "status": "approved", "strict": True}
        changed.append(sale_key)
    return changed


def _live(approval: dict, now: datetime | None = None) -> bool:
    if approval.get("status") != "approved" or not approval.get("expires_at"):
        return False
    now = now or datetime.now(TZ)
    now = now.astimezone(TZ) if now.tzinfo else now.replace(tzinfo=TZ)
    return now < datetime.fromisoformat(approval["expires_at"])


def listing_candidates(state: dict, me: dict, tick_seconds: float | None,
                       top_teams: set[str] | None, unavailable_assets: set | None = None) -> list[dict]:
    if not tick_seconds or tick_seconds <= 0:
        return []
    by_ref: dict[str, list] = {}
    for asset in me.get("assets", []):
        if asset.get("kind") == "card" and asset.get("ref"):
            by_ref.setdefault(asset["ref"], []).append(asset)
    reserved = {str(a.get("asset_id")) for a in state.get("approvals", {}).values()
                if a.get("status") in ("posting", "posted", "settled") and a.get("asset_id") is not None}
    reserved.update(str(a) for a in (unavailable_assets or set()))
    out = []
    for key, approval in sorted(state.get("approvals", {}).items()):
        if not _live(approval) or approval.get("offer_id") is not None:
            continue
        copies = by_ref.get(approval["card"], [])
        if len(copies) < 2:
            approval["last_blocker"] = "no hay duplicado disponible"
            continue
        asset = next((a for a in sorted(copies, key=lambda x: str(x.get("id")))
                      if str(a.get("id")) not in reserved), None)
        if asset is None:
            approval["last_blocker"] = "las copias están comprometidas"
            continue
        remaining = (datetime.fromisoformat(approval["expires_at"]) - datetime.now(TZ)).total_seconds()
        expires_in = int(remaining // tick_seconds) - 1
        if expires_in < 1:
            approval["last_blocker"] = "queda menos de un tick antes del vencimiento"
            continue
        approval["asset_id"] = asset["id"]
        reserved.add(str(asset["id"]))
        blockers = []
        if top_teams is None or approval["buyer"] in top_teams:
            blockers.append("comprador top 6: la API no permite comprobar si esta venta completa su página")
        out.append({"type": "list", "module": "comision v10", "kind": "venta aprobada",
                    "asset": asset["id"], "ref": f"card:{approval['card']}", "price": approval["price"],
                    "venue": approval.get("venue", VENUE), "to": approval["buyer"], "expires_in": expires_in,
                    "v10_approval": True, "approval_key": key, "score": 4.5 * 10 ** 5, "du": None,
                    "blockers": blockers, "reason": f"autorización #{THREAD_ID}/{MESSAGE_ID}"})
    return out


def validate_candidate(candidate: dict, state: dict) -> tuple[bool, str | None]:
    approval = state.get("approvals", {}).get(candidate.get("approval_key"))
    if not approval or not _live(approval):
        return False, "aprobación ausente o vencida"
    actual = (candidate.get("to"), candidate.get("price"),
              str(candidate.get("ref", "")).removeprefix("card:"), candidate.get("asset"), candidate.get("venue"))
    expected = (approval["buyer"], approval["price"], approval["card"], approval.get("asset_id"),
                approval.get("venue", VENUE))
    if candidate.get("type") != "list" or not candidate.get("v10_approval") or actual != expected:
        return False, "carta, comprador, precio, copia o venue no coincide con la autorización"
    return True, None


def reconcile_open_offers(state: dict, offers: list, team: str) -> list[str]:
    changed = []
    for key, approval in state.get("approvals", {}).items():
        if approval.get("status") != "posting" or approval.get("offer_id") is not None:
            continue
        matches = [o for o in offers if o.get("maker") == team and o.get("venue") == VENUE
                   and o.get("to") == approval["buyer"] and o.get("status") in ("open", "queued")
                   and o.get("want", {}).get("cash") == approval["price"]
                   and any(str(a.get("id") if isinstance(a, dict) else a) == str(approval.get("asset_id"))
                           for a in o.get("give", {}).get("assets", []))]
        if len(matches) == 1:
            approval.update(status="posted", offer_id=matches[0].get("id"), recovered=True)
            changed.append(key)
        elif len(matches) > 1:
            approval.update(status="ambiguous", error="varias ofertas coinciden; revisar")
            changed.append(key)
    return changed


def reconcile_feed(state: dict, events: list, team: str) -> list[dict]:
    by_offer = {str(a["offer_id"]): a for a in state.get("approvals", {}).values()
                if a.get("offer_id") is not None}
    seen = {str(a.get("settlement_id")) for a in state.get("approvals", {}).values()
            if a.get("settlement_id") is not None}
    changed = []
    for event in events or []:
        if event.get("type") != "settlement":
            continue
        payload = event.get("payload") or {}
        oid = payload.get("offer_id", payload.get("offer"))
        approval = by_offer.get(str(oid)) if oid is not None else None
        if not approval:
            continue
        buyer = str(payload.get("buyer") or "").lower()
        card_out = any(i.get("kind") == "card" and i.get("ref") == approval["card"]
                       and i.get("frm") == team and str(i.get("to", "")).lower() == approval["buyer"]
                       for i in payload.get("items", []))
        parties = {str(p).lower() for p in payload.get("parties", [])}
        exact = (payload.get("venue") == approval.get("venue", VENUE) and payload.get("price") == approval["price"]
                 and (buyer == approval["buyer"] or approval["buyer"] in parties)
                 and team in parties and approval["buyer"] in parties and card_out)
        settlement = payload.get("settlement", event.get("id"))
        if not exact:
            approval.update(status="terms_mismatch", mismatch_event=event.get("id"))
            changed.append({"offer_id": oid, "status": "terms_mismatch"})
        elif str(settlement) not in seen:
            approval.update(status="settled", settlement_id=settlement,
                            settled_tick=payload.get("tick", event.get("tick")))
            seen.add(str(settlement))
            changed.append({"offer_id": oid, "settlement_id": settlement, "status": "settled", "commission": 1})
    return changed


def summary(state: dict) -> dict:
    approvals = list(state.get("approvals", {}).values())
    settled = {str(a.get("settlement_id")) for a in approvals
               if a.get("status") == "settled" and a.get("settlement_id") is not None}
    paid = sum(int(a.get("commission_paid_p") or 0) for a in approvals if a.get("status") == "settled")
    return {"approved": sum(a.get("status") in ("approved", "posting", "posted", "settled") for a in approvals),
            "posted": sum(a.get("status") in ("posted", "settled") for a in approvals),
            "settled_sales": len(settled), "commission_due_p": len(settled),
            # 1 P prometido NO es efectivo: pendiente = debido − cobrado confirmado; solo cuenta lo que el servidor liquida
            "commission_paid_confirmed_p": paid, "commission_pending_p": max(0, len(settled) - paid),
            "commission_counts_as_cash": False,
            # operaciones realmente aportadas por Team 5 (ventas nuestras liquidadas por su venue/compradores aprobados)
            "reciprocal_sales_contributed": len(settled),
            "expired_unused": sum(a.get("status") == "approved" and not _live(a) for a in approvals)}
