"""Las tres fuentes de la radio, su fiabilidad y qué hacer con cada una.

Qué estaba mal
--------------
`radio_event()` filtraba `payload["source"] != "radio"`, así que **el Boletín del
Bazar y El Tablón se descartaban enteros**. Y la lista venía del almacén local del
feed, que tiene 1 evento `news.posted`, cuando `GET /api/news` devuelve 8 en tres
fuentes. El panel mostraba como máximo 1 de 8, y por construcción nunca podía
mostrar ni el boletín oficial ni el tablón de cebos.

Eso invierte el sentido de la sección: el boletín es la única fuente cierta (ahí
se anunció el sobre gratis), y El Tablón es cebo que puede provocar un
`cooloff` con el vendedor si se actúa. Justo las dos que no se veían.

La ventana también estaba mal: `current_tick > tick + 120` supone 30 s por tick,
pero el viernes iba a 60 s y el domingo va a 15 s. `at_hours` es el reloj del
juego y no depende del ritmo de ticks.

Fiabilidad por fuente (RULES.md, «The radio»)
---------------------------------------------
  boletin  Boletín del Bazar  oficial: lo que dice ocurre          -> ACTUAR
  radio    Radio Rastro       rumor, normalmente con plazo          -> VERIFICAR
  tablon   El Tablón          cebo y chismorreo, nunca cierto       -> IGNORAR
"""
from __future__ import annotations

import re
import unicodedata

VERDICTS = ("act", "verify", "ignore", "expired", "noise")

SOURCES = {
    "boletin": {"label": "Boletín del Bazar", "reliability": "oficial", "verdict": "act",
                "why": "Fuente oficial del Bazar: lo que anuncia ocurre."},
    "radio": {"label": "Radio Rastro", "reliability": "rumor", "verdict": "verify",
              "why": "Rumor con plazo. Contrasta con una puja o cotización real antes de mover precios."},
    "tablon": {"label": "El Tablón", "reliability": "cebo", "verdict": "ignore",
               "why": "Chismorreo y cebo. Actuar sobre esto puede provocar un enfriamiento con el vendedor."},
}

DAY_END_HOURS = 14.0  # el sábado cierra a las 23:00; las horas de juego arrancan a las 09:00

RARITIES = ((r"\b(legendary|legendaria|legendarias)\b", "legendary"),
            (r"\b(epic|epica|epicas)\b", "epic"),
            (r"\b(rare|raras?|raros?)\b", "rare"),
            (r"\b(uncommon|infrecuentes?)\b", "uncommon"),
            (r"\b(common|comunes?)\b", "common"))


def normalized(value: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFKD', value.lower())
                   if not unicodedata.combining(c))


def news_item(raw: dict) -> dict:
    """Normaliza una fila de `GET /api/news`, que trae las tres fuentes."""
    source = raw.get("source") or "radio"
    return {"id": raw.get("id"), "tick": raw.get("tick"), "at_hours": raw.get("at_hours"),
            "source_id": source, "source": raw.get("source_name") or SOURCES.get(source, {}).get("label") or source,
            "headline": raw.get("headline") or "", "body": raw.get("body") or ""}


def radio_event(event: dict) -> dict | None:
    """Camino del feed (`news.posted`), ahora SIN filtrar por fuente.

    Se mantiene porque el feed llega en vivo por SSE y `GET /api/news` hay que
    sondearlo, pero ya no descarta el boletín ni el tablón."""
    if event.get("type") != "news.posted":
        return None
    payload = event.get("payload") or {}
    return news_item({**payload, "id": payload.get("id", event.get("id")),
                      "tick": payload.get("tick", event.get("tick"))})


def window(item: dict) -> dict:
    """Plazo declarado en el texto, en horas de juego.

    Se lee de `at_hours` y no de los ticks: el ritmo cambia por día (60 s el viernes,
    30 s el sábado, 15 s el domingo), así que contar ticks da plazos falsos."""
    words = normalized(item.get("headline", "") + " " + item.get("body", ""))
    start = item.get("at_hours")
    if not isinstance(start, (int, float)):
        return {"declared": None, "expires_at_hours": None}
    if re.search(r"\b(one hour|in one hour|una hora|no more)\b", words):
        return {"declared": "una hora", "expires_at_hours": round(start + 1.0, 4)}
    if re.search(r"\b(half an hour|media hora)\b", words):
        return {"declared": "media hora", "expires_at_hours": round(start + 0.5, 4)}
    if re.search(r"\b(today|from today|hoy)\b", words):
        return {"declared": "hoy", "expires_at_hours": DAY_END_HOURS}
    return {"declared": None, "expires_at_hours": None}


def contradicted_by_dealers(item: dict, dealers: dict) -> str | None:
    """Refuta una noticia contra `/api/dealers`, que es autoritativo sobre qué compran.

    El caso vivo: El Tablón dice «Abuela deja de comprar comunes» mientras su menú
    sigue listando `common`. Un cebo refutable con un endpoint no debería llegar al
    panel como una noticia más."""
    words = normalized(item.get("headline", "") + " " + item.get("body", ""))
    if not re.search(r"\b(stops?|deja de|ya no)\b", words):
        return None
    for did, dealer in (dealers or {}).items():
        name = normalized(dealer.get("name") or did)
        if did not in words and not any(part in words for part in name.split() if len(part) > 3):
            continue
        buys = [r.get("rarity") for r in (dealer.get("menu") or {}).get("buys", []) if r.get("rarity")]
        for pattern, rarity in RARITIES:
            if re.search(pattern, words) and rarity in buys:
                return (f"/api/dealers contradice la noticia: el menú de {dealer.get('name') or did} "
                        f"sigue comprando {rarity}.")
    return None


def interpret(news: list[dict], catalog: dict, guide: list[dict], current_tick: int = 0,
              t_hours: float | None = None, holdings: dict | None = None,
              dealers: dict | None = None, limit: int = 8) -> list[dict]:
    """Una fila por noticia: fuente, fiabilidad, plazo, a qué cartas nuestras toca y qué hacer.

    `guide` son los duplicados libres y `holdings` todo lo que tenemos ({ref: stock}).
    Un rumor de «compra raras de Malasaña» importa por lo que TENEMOS, no sólo por lo
    que sobra."""
    cards = {c["id"]: c for s in catalog.get("sets", []) for c in s.get("cards", [])}
    sets = {s["id"]: s for s in catalog.get("sets", [])}
    held = {ref for ref, n in (holdings or {}).items() if n}
    free = {g["ref"] for g in guide or []}
    out = []
    ordered = sorted(news or [], key=lambda n: (n.get("at_hours") or 0, n.get("id") or 0), reverse=True)
    for item in ordered[:limit]:
        policy = SOURCES.get(item.get("source_id") or "radio", SOURCES["radio"])
        text = item.get("headline", "") + " " + item.get("body", "")
        words = normalized(text)
        named_sets = {sid for sid, s in sets.items()
                      if (s.get("name") and normalized(s["name"]) in words)
                      or re.search(r"\b" + re.escape(sid.lower()) + r"\b", words)}
        rarity = next((value for pattern, value in RARITIES if re.search(pattern, words)), None)
        explicit = set(re.findall(r"\b[A-Z]{3}-\d{2}\b", text.upper()))
        touched = sorted({ref for ref in cards
                          if ref in explicit
                          or (ref.split("-")[0] in named_sets
                              and (rarity is None or cards[ref].get("rarity") == rarity))})
        ours = [r for r in touched if r in held]
        sellable = [r for r in ours if r in free]
        win = window(item)
        expired = (win["expires_at_hours"] is not None and t_hours is not None
                   and t_hours > win["expires_at_hours"])
        refutation = contradicted_by_dealers(item, dealers or {})

        if refutation:
            verdict, action = "ignore", refutation
        elif policy["verdict"] == "ignore":
            verdict, action = "ignore", policy["why"]
        elif expired:
            verdict = "expired"
            action = (f"El plazo de «{win['declared']}» venció en la hora {win['expires_at_hours']:.2f} "
                      f"y vamos por la {t_hours:.2f}. No mueve precios.")
        elif policy["verdict"] == "act":
            # El boletín va antes de la comprobación de cartas: un anuncio oficial (un sobre
            # para cada equipo, un cambio de reglas) es accionable aunque no nombre ninguna carta.
            verdict = "act"
            action = ("Fuente oficial" + (f" y toca {', '.join(ours)}" if ours else "")
                      + (f". Plazo: {win['declared']}, hasta la hora {win['expires_at_hours']:.2f}."
                         if win["expires_at_hours"] is not None else "."))
        elif not touched:
            verdict = "noise"
            action = "No menciona cartas ni vendedores: no cambia ningún precio."
        elif ours:
            verdict = "verify"
            action = (f"Tenemos {', '.join(ours)}"
                      + (f" ({len(sellable)} con copia libre)" if sellable else " sin copia libre")
                      + ". Pide una cotización real antes de mover el precio; el rumor no confirma la puja."
                      + (f" Plazo: {win['declared']}, hasta la hora {win['expires_at_hours']:.2f}."
                         if win["expires_at_hours"] is not None else ""))
        else:
            verdict = "noise"
            action = (f"Menciona {', '.join(touched[:4])}, que no tenemos. "
                      "No concedas ni subas precios por este rumor.")

        out.append({**item, "reliability": policy["reliability"], "verdict": verdict,
                    "source_note": policy["why"], "window": win, "expired": bool(expired),
                    "touches": touched, "ours": ours, "sellable": sellable,
                    "matches": ours, "action": action})
    return out


def radio_summary(items: list[dict]) -> dict:
    """Recuento por veredicto, para no tener que leer ocho tarjetas."""
    counts = {v: 0 for v in VERDICTS}
    for item in items or []:
        counts[item.get("verdict", "noise")] = counts.get(item.get("verdict", "noise"), 0) + 1
    live = [i for i in (items or []) if i.get("verdict") in ("act", "verify")]
    return {"total": len(items or []), "by_verdict": counts,
            "actionable": [{"source": i["source"], "headline": i["headline"],
                            "verdict": i["verdict"], "ours": i.get("ours") or []} for i in live],
            "bait_present": counts.get("ignore", 0) > 0}
