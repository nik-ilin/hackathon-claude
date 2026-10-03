"""RADIO E INTELIGENCIA PÚBLICA: captura de /api/news, registro idempotente y correlación PRUDENTE con eventos públicos posteriores.
Módulo opcional del dashboard (lo carga `dashboard.load_optional`); stdlib salvo `news_watch.classify` si está disponible.

Fuente real: `GET /api/news` (público, sin clave) devuelve TODO el historial de noticias (id, tick, at_hours, source, headline, body) y el feed
público trae el mismo mensaje como `news.posted` (payload.id = id de la noticia; su id de evento es la evidencia enlazable). No existe otro
canal de radio. No hay endpoint de «audiencia»: no se sabe quién la oyó.

Almacenamiento: `data/radio_intel.jsonl`, SOLO se añade (nunca se reescribe): `msg` (una vez por id), `sighting` (una vez por vía), `review`
(marcas de revisado, gana la última) y `poll` (errores y altas). Reiniciar el monitor relee el fichero y no duplica nada.

Principios:
  · El texto original (headline/body) se guarda tal cual y es DATO no confiable: jamás se interpreta como instrucción. Toda clasificación
    (temas, entidades, tipo) vive aparte, marcada como INFERENCIA.
  · Correlación = «posible conexión»: coincidencia temporal (el evento es posterior al mensaje y cae en su ventana) + temática (vendedor,
    barrio/rareza/carta, equipo, comisión/venue), con su criterio, su línea base (actividad previa comparable) y una confianza. Nunca causalidad.
  · No observable (el feed no lo publica): el texto de las conversaciones con vendedores (`text: null`), si un equipo oyó la radio, consultas
    privadas. Se dice expresamente en cada línea temporal.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Iterable, Optional

try:                                            # clasificador existente; si falta, un mínimo propio
    import news_watch as _nw
except Exception:                               # noqa: BLE001
    _nw = None

TICKS_PER_HOUR = 120.0
DEFAULT_WINDOW = 120           # ticks (1 h de juego) si el mensaje no declara duración
MAX_WINDOW = 360
BASELINE_MIN_TICKS = 30        # historia previa mínima para hablar de línea base
MAX_CANDIDATES = 12
SOURCES = {"radio": "Radio Rastro", "boletin": "Boletín del Bazar", "tablon": "El Tablón (rumores)"}
CARD = re.compile(r"\b([A-Z]{3}-\d{1,2})\b")
TEAM = re.compile(r"\b(?:team|equipo)\s*(\d{1,2})\b|\b(t\d{1,2})\b", re.I)
VENUE = re.compile(r"\b(v\d{1,2})\b|\b(el rastro|el duende|rastro|duende)\b", re.I)
FEE = re.compile(r"\b(fee|fees|commission|commissions|comisi[oó]n|comisiones|tax|charge)\b", re.I)
PACK = re.compile(r"\b(packs?|sobres?)\b", re.I)
MENU = re.compile(r"\b(menu|menú|stock|vault|sells?|buys?|pays?|paying|price|prices)\b", re.I)
EVENT = re.compile(r"\b(duels?|market test|bench|announcement|payday|unlocks?|opens?|closes?|released?|reprinted|reprint)\b", re.I)
IRRELEVANT = re.compile(r"\b(degrees|sunny|storm|rain|metro|queue|churro|weather|goal|match|atleti)\b", re.I)


# ------------------------------------------------------------------ clasificación (INFERENCIA, separada del texto)

def classify(item: dict, dealers: Iterable[str] = (), catalog: Optional[dict] = None) -> dict:
    text = f"{item.get('headline') or ''}. {item.get('body') or ''}"
    low = text.lower()
    base = {}
    if _nw is not None:
        try:
            c = _nw.classify(item, catalog)
            base = {k: c.get(k) for k in ("kind", "dealer", "set", "rarity", "item", "window_hours", "negated", "time_ambiguous",
                                         "time_text", "reason")}
        except Exception:                       # noqa: BLE001
            base = {}
    dealer_ids = sorted({base.get("dealer")} - {None} | {d for d in dealers if d and re.search(rf"\b{re.escape(d)}\b", low)})
    sets = sorted({base.get("set")} - {None} | set(CARD.findall(text) and {c[:3] for c in CARD.findall(text)}))
    cards = sorted(set(CARD.findall(text)))
    teams = sorted({f"t{int(a or b[1:]):02d}" for a, b in TEAM.findall(text)})
    venues = sorted({(a or b).lower() for a, b in VENUE.findall(text)})
    topics = []
    if dealer_ids:
        topics.append("vendedor")
    if cards or sets:
        topics.append("carta")
    if base.get("rarity"):
        topics.append("rareza")
    if teams:
        topics.append("equipo")
    if FEE.search(text):
        topics.append("comisión/fee")
    if venues:
        topics.append("mercado")
    if MENU.search(text) and (dealer_ids or base.get("kind") in ("demanda", "oferta")):
        topics.append("menú")
    if PACK.search(text) or base.get("kind") == "regalo":
        topics.append("sobres/regalo")
    if EVENT.search(text):
        topics.append("evento")
    if not topics and (IRRELEVANT.search(text) or base.get("kind") == "irrelevante"):
        topics.append("ambiente")
    return {"topics": topics or ["sin clasificar"], "dealers": dealer_ids, "sets": sets, "rarities": [base["rarity"]] if base.get("rarity") else [],
            "cards": cards, "teams": teams, "venues": venues, "kind": base.get("kind"), "negated": bool(base.get("negated")),
            "time_ambiguous": bool(base.get("time_ambiguous")), "window_hours": base.get("window_hours"),
            "reason": base.get("reason"), "label": "INFERENCIA (clasificación automática; no sustituye al texto original)"}


# ------------------------------------------------------------------ almacén idempotente (solo añade)

class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.msgs: dict = {}          # key → registro
        self.sightings: dict = {}     # key → {via: record}
        self.reviews: dict = {}       # key → bool (la última manda)
        self.polls: list = []
        self._load()

    def _load(self) -> None:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines:
            try:
                r = json.loads(line)
            except ValueError:
                continue                  # una línea truncada no invalida el resto
            k = r.get("k")
            if k == "msg" and r.get("key") and r["key"] not in self.msgs:
                self.msgs[r["key"]] = r
            elif k == "sighting" and r.get("key"):
                self.sightings.setdefault(r["key"], {}).setdefault(r.get("via"), r)
            elif k == "review" and r.get("key"):
                self.reviews[r["key"]] = bool(r.get("on"))
            elif k == "poll":
                self.polls.append(r)
        self.polls = self.polls[-50:]

    def _append(self, rec: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def add_msg(self, rec: dict) -> bool:
        if rec["key"] in self.msgs:
            return False
        self.msgs[rec["key"]] = rec
        self._append(rec)
        return True

    def add_sighting(self, key: str, via: str, **kw) -> bool:
        if via in self.sightings.get(key, {}):
            return False
        rec = dict({"k": "sighting", "key": key, "via": via, "ts": round(time.time(), 1)}, **kw)
        self.sightings.setdefault(key, {})[via] = rec
        self._append(rec)
        return True

    def set_review(self, key: str, on: bool) -> bool:
        if key not in self.msgs:
            return False
        if self.reviews.get(key, False) != bool(on):
            self.reviews[key] = bool(on)
            self._append({"k": "review", "key": key, "on": bool(on), "ts": round(time.time(), 1)})
        return True

    def add_poll(self, rec: dict) -> None:
        self.polls.append(rec)
        self.polls = self.polls[-50:]
        self._append(rec)


def _item_text(it: dict) -> str:
    return it.get("text") or ": ".join(x for x in (it.get("source_name"), it.get("headline")) if x)


# ------------------------------------------------------------------ eventos públicos compatibles

def _event_summary(e: dict) -> dict:
    p = e.get("payload") or {}
    t = e.get("type")
    d = {"id": e.get("id"), "tick": e.get("tick"), "type": t, "actor": e.get("actor") or p.get("team") or p.get("sender")}
    if t in ("thread.opened", "thread.message", "thread.closed"):
        d.update(team=p.get("team"), dealer=p.get("with") if p.get("kind") == "persona" else None, kind=p.get("kind"),
                 topic=p.get("topic"), has_text=bool(p.get("text")), offer=bool(p.get("offer")), thread=p.get("thread"))
    elif t == "settlement":
        it = (p.get("items") or [{}])[0]
        d.update(dealer=p.get("persona"), ref=it.get("ref"), set=it.get("set"), rarity=it.get("rarity"), price=p.get("price"),
                 venue=p.get("venue"), seller=it.get("frm"), buyer=it.get("to"), settlement=p.get("settlement"))
    elif t in ("offer.listed",):
        o = p.get("offer") or {}
        g, w = o.get("give") or {}, o.get("want") or {}
        ref = next((a.get("ref") for a in g.get("assets") or [] if isinstance(a, dict)), None) or \
            next((x[5:] for x in w.get("types") or [] if str(x).startswith("card:")), None)
        d.update(team=o.get("maker"), ref=ref, venue=o.get("venue"), offer=o.get("id"))
    elif t == "gift.given":
        d.update(dealer=e.get("actor"), team=p.get("team"), cards=p.get("cards"), packs=p.get("packs"))
    elif t.startswith("venue."):
        d.update(venue=p.get("venue"), fee_bps=p.get("fee_bps"), fee_per_card=p.get("fee_per_card"))
    elif t in ("level.unlocked", "persona.updated", "persona.open_to_all"):
        d.update(dealer=p.get("persona"), team=p.get("team"))
    return d


INTERESTING = {"thread.opened", "thread.message", "settlement", "offer.listed", "gift.given", "venue.fee_announced", "venue.fee_changed",
               "venue.announcement", "level.unlocked", "persona.updated", "persona.open_to_all"}


def match_event(msg: dict, ev: dict) -> list:
    """Criterios por los que `ev` encaja temáticamente con el mensaje (lista vacía = no encaja). No mira el tiempo."""
    inf, out = msg["inferred"], []
    t = ev["type"]
    if ev.get("dealer") and ev["dealer"] in inf["dealers"]:
        out.append(f"vendedor {ev['dealer']}")
    if t in ("settlement", "offer.listed"):
        if ev.get("ref") and ev["ref"] in inf["cards"]:
            out.append(f"carta {ev['ref']}")
        elif ev.get("ref") and ev["ref"][:3] in inf["sets"]:
            out.append(f"barrio {ev['ref'][:3]}")
        if ev.get("rarity") and ev["rarity"] in inf["rarities"]:
            out.append(f"rareza {ev['rarity']}")
    if inf["teams"] and (ev.get("team") in inf["teams"] or ev.get("actor") in inf["teams"] or ev.get("seller") in inf["teams"]
                         or ev.get("buyer") in inf["teams"]):
        out.append("equipo mencionado")
    if t.startswith("venue.") and ("comisión/fee" in inf["topics"] or inf["venues"]):
        if "comisión/fee" in inf["topics"] or (ev.get("venue") or "") in inf["venues"]:
            out.append("comisión/mercado")
    return out


def correlate(msg: dict, events: list, tick_now: Optional[int], first_event_tick: Optional[int], seen_live: Optional[dict] = None) -> dict:
    """Línea temporal de UN mensaje: emisión → eventos públicos compatibles → resultado conocido. Todo con criterio y confianza."""
    tm = msg.get("tick")
    inf = msg["inferred"]
    w_h = inf.get("window_hours")
    window = int(min(MAX_WINDOW, (w_h or 0) * TICKS_PER_HOUR)) if w_h else DEFAULT_WINDOW
    base = {"emission": {"tick": tm, "captured_ts": msg.get("captured_ts"), "first_seen_tick": msg.get("first_seen_tick")},
            "window_ticks": window, "candidates": [], "not_observable": [], "result": None, "method": ""}
    if tm is None or not (inf["dealers"] or inf["cards"] or inf["sets"] or inf["rarities"] or inf["teams"] or inf["venues"]
                          or "comisión/fee" in inf["topics"]):
        base["method"] = "el mensaje no nombra vendedor, carta, barrio, rareza, equipo, venue ni comisión: no hay criterio para emparejar"
        return base
    if inf["dealers"]:
        base["not_observable"].append(
            f"texto de las consultas a {', '.join(inf['dealers'])}: no observable (el feed publica la apertura del hilo y las ofertas "
            f"estructuradas, no los mensajes: `text` llega vacío)")
    base["not_observable"].append("si algún equipo oyó este mensaje: no observable (no hay datos de audiencia)")
    after = [e for e in events if e.get("tick") is not None and tm <= e["tick"] <= tm + window]
    pre = [e for e in events if e.get("tick") is not None and tm - window <= e["tick"] < tm]
    have_base = first_event_tick is not None and first_event_tick <= tm - BASELINE_MIN_TICKS
    cands = []
    for e in sorted(after, key=lambda x: (x["tick"], x.get("id") or 0)):
        crit = match_event(msg, e)
        if not crit:
            continue
        cands.append(dict(e, criteria=crit, delta_ticks=e["tick"] - tm))
    pre_match = [e for e in pre if match_event(msg, e)]
    n_post, n_pre = len(cands), len(pre_match)
    lift = round(n_post / max(n_pre, 0.5), 2) if have_base else None
    for c in cands:
        specific = sum(1 for x in c["criteria"] if x.split()[0] in ("carta", "rareza", "barrio", "equipo", "comisión/mercado"))
        generic = any(x.startswith("vendedor") for x in c["criteria"])
        if specific and generic and c["delta_ticks"] <= 60 and (lift or 0) >= 1.5:
            conf = "media-alta"
        elif (specific or generic) and have_base and (lift or 0) >= 1.2:
            conf = "media"
        else:
            conf = "baja"
        if not have_base:
            conf = "baja"
        c["confidence"] = conf
        c["note"] = ("posible conexión: coincide en " + ", ".join(c["criteria"]) + f"; {c['delta_ticks']} ticks después"
                     + ("" if have_base else "; sin línea base previa suficiente"))
        if seen_live and c.get("id") in seen_live:
            c["observed_ts"] = seen_live[c["id"]]
    base["candidates"] = cands[:MAX_CANDIDATES]
    base["n_candidates"] = n_post
    base["baseline"] = {"events_before": n_pre, "events_after": n_post, "lift": lift,
                        "note": ("actividad comparable en la ventana previa; si es parecida, la conexión es indistinguible de lo habitual"
                                 if have_base else "sin eventos públicos anteriores suficientes: no se puede estimar lo habitual")}
    base["method"] = (f"eventos públicos POSTERIORES en {window} ticks que comparten vendedor/carta/barrio/rareza/equipo/comisión con el "
                      "mensaje; confianza por especificidad, cercanía y aumento frente a la ventana previa. No prueba causalidad")
    settlements = [c for c in cands if c["type"] == "settlement"]
    base["result"] = ({"settlements": [{"id": c["id"], "tick": c["tick"], "ref": c.get("ref"), "price": c.get("price"), "venue": c.get("venue")}
                                       for c in settlements[:6]], "note": "liquidaciones públicas compatibles (resultado conocido)"}
                      if settlements else {"note": "sin liquidación pública compatible en la ventana"})
    if not cands:
        base["no_events"] = "ningún evento público compatible tras el mensaje"
    return base


# ------------------------------------------------------------------ verificación (hecho / rumor / inferencia)

def verification(msg: dict, dealers_data: Optional[dict], timeline: dict, events: list) -> dict:
    inf, src = msg["inferred"], msg.get("source")
    ev = []
    status = "sin_verificar"
    kind = inf.get("kind")
    if kind in ("demanda", "oferta") and inf["dealers"] and dealers_data and _nw is not None:
        d = (dealers_data or {}).get(inf["dealers"][0])
        side = "buys" if kind == "demanda" else "sells"
        row = _nw.explicit_menu_row(d, side, (inf["sets"] or [None])[0], (inf["rarities"] or [None])[0]) if d else None
        if row:
            status, ev = "confirmado", [f"menú actual de {inf['dealers'][0]} (/api/dealers) tiene una fila explícita {side}"]
    if kind == "regalo" and inf["dealers"]:
        tm = msg.get("tick") or 0
        gifts = [e for e in events if e["type"] == "gift.given" and e.get("dealer") in inf["dealers"] and tm <= (e["tick"] or 0) <= tm + 120]
        if gifts:
            status, ev = "confirmado", [f"gift.given de {inf['dealers'][0]} en feed (evento {g['id']}, tick {g['tick']})" for g in gifts[:3]]
    if status != "confirmado" and any(c["type"].startswith("venue.fee") for c in timeline.get("candidates", [])) and "comisión/fee" in inf["topics"]:
        status, ev = "confirmado", [f"cambio de comisión público (evento {c['id']}, tick {c['tick']})" for c in timeline["candidates"]
                                    if c["type"].startswith("venue.fee")][:3]
    if status != "confirmado":
        if src == "tablon":
            status = "rumor_no_confirmado"
        elif inf["topics"] == ["ambiente"]:
            status = "sin_efecto_de_mercado"
        elif inf["topics"] == ["sin clasificar"]:
            status = "sin_verificar"
    return {"status": status, "evidence": ev, "literal": True,
            "labels": {"literal": "texto original del servidor", "confirmado": "hecho confirmado por feed/API",
                       "rumor_no_confirmado": "rumor no confirmado (El Tablón)", "inferencia": "clasificación automática"}}


# ------------------------------------------------------------------ alertas (informan; no actúan)

def alerts_for(msg: dict, monitored: dict) -> list:
    inf, out = msg["inferred"], []
    key = lambda kind, ent: f"alert:{msg['key']}:{kind}:{ent}"
    for d in inf["dealers"]:
        if d in monitored.get("dealers", ()):
            out.append({"key": key("vendedor", d), "kind": "vendedor", "entity": d, "text": f"menciona al vendedor {d}"})
    for c in inf["cards"]:
        out.append({"key": key("carta", c), "kind": "carta", "entity": c, "text": f"menciona la carta {c}"})
    for s in inf["sets"]:
        if s in monitored.get("sets", ()) or not monitored.get("sets"):
            out.append({"key": key("barrio", s), "kind": "carta", "entity": s, "text": f"menciona el barrio {s}"})
    for r in inf["rarities"]:
        out.append({"key": key("rareza", r), "kind": "rareza", "entity": r, "text": f"menciona rareza {r}"})
    if "menú" in inf["topics"]:
        out.append({"key": key("menú", "menú"), "kind": "menú", "entity": "menú", "text": "puede afectar al menú de un vendedor"})
    if "comisión/fee" in inf["topics"]:
        out.append({"key": key("fee", "fee"), "kind": "comisión/fee", "entity": "fee", "text": "habla de comisiones o fees"})
    for v in inf["venues"]:
        out.append({"key": key("mercado", v), "kind": "mercado", "entity": v, "text": f"menciona el mercado {v}"})
    if "evento" in inf["topics"]:
        out.append({"key": key("evento", "evento"), "kind": "evento", "entity": "evento", "text": "habla de un evento del juego"})
    seen, uniq = set(), []
    for a in out:
        if a["key"] not in seen:                  # una alerta por (mensaje, tipo, entidad), nunca duplicadas
            seen.add(a["key"])
            uniq.append(dict(a, news_key=msg["key"], rumor=msg.get("source") == "tablon",
                             advice="informativa: no envía ofertas, no cambia precios ni activa decisiones"))
    return uniq


# ------------------------------------------------------------------ servicio

def load_memory_events(path, since_tick: int = 0, limit: int = 30000) -> list:
    """Eventos públicos de la memoria existente (agent_memory.sqlite3, solo lectura) de los tipos que se correlacionan. [] si no se puede leer."""
    out = []
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        try:
            for (body,) in db.execute("SELECT body FROM events WHERE tick >= ? ORDER BY tick, id LIMIT ?", (since_tick, limit)):
                try:
                    e = json.loads(body)
                except ValueError:
                    continue
                if e.get("type") in INTERESTING:
                    out.append(e)
        finally:
            db.close()
    except Exception:                              # noqa: BLE001 — la memoria es opcional
        return []
    return out


class RadioIntel:
    def __init__(self, path, memory_path=None, poll_every: float = 30.0):
        self.store = Store(path)
        self.memory_path = memory_path
        self.poll_every = poll_every
        self._last_poll = 0.0
        self.status = {"last_ok_ts": None, "last_error": None, "last_poll_ts": None, "last_new": 0, "polls": 0}
        self._mem_ts = 0.0
        self._mem_events: list = []

    # --- ingestión
    def ingest_items(self, items: list, tick: Optional[int], via: str = "api", event_ids: Optional[dict] = None, now: Optional[float] = None,
                     dealers: Iterable[str] = (), catalog: Optional[dict] = None) -> int:
        now = time.time() if now is None else now
        new = 0
        for it in items or []:
            if not isinstance(it, dict) or it.get("id") is None:
                continue                           # sin id estable no se inventa uno
            key = f"news:{it['id']}"
            ev_id = (event_ids or {}).get(it["id"])
            if key not in self.store.msgs:
                rec = {"k": "msg", "key": key, "id": it["id"], "source": it.get("source"), "source_name": it.get("source_name"),
                       "headline": it.get("headline"), "body": it.get("body"), "text": _item_text(it), "tick": it.get("tick"),
                       "at_hours": it.get("at_hours"), "captured_ts": round(now, 1), "first_seen_tick": tick, "via": via,
                       "provenance": ("/api/news" if via == "api" else f"feed news.posted#{ev_id}"), "event_id": ev_id}
                if self.store.add_msg(rec):
                    new += 1
            else:
                self.store.add_sighting(key, via, event_id=ev_id, tick=tick)
        return new

    def ingest_events(self, events: Iterable[dict], tick: Optional[int] = None, now: Optional[float] = None) -> int:
        items, ids = [], {}
        for e in events or []:
            if e.get("type") == "news.posted":
                p = e.get("payload") or {}
                if p.get("id") is not None:
                    items.append({"id": p["id"], "tick": e.get("tick"), "at_hours": e.get("t"), "source": p.get("source"),
                                  "source_name": p.get("source_name"), "headline": p.get("headline"), "body": p.get("body"), "text": p.get("text")})
                    ids[p["id"]] = e.get("id")
        return self.ingest_items(items, tick, "feed", ids, now)

    def poll(self, fetch, tick: Optional[int] = None, now: Optional[float] = None, dealers: Iterable[str] = (), force: bool = False) -> dict:
        """Sondea /api/news como mucho cada `poll_every` s (un tick). Un fallo se registra y se muestra; no detiene nada."""
        now = time.time() if now is None else now
        if not force and now - self._last_poll < self.poll_every:
            return dict(self.status, skipped=True)
        self._last_poll = now
        self.status["polls"] += 1
        self.status["last_poll_ts"] = now
        try:
            data = fetch()
            items = data.get("news") if isinstance(data, dict) else data
            if not isinstance(items, list):
                raise ValueError("respuesta de /api/news sin lista `news`")
            new = self.ingest_items(items, tick, "api", None, now, dealers)
            self.status.update(last_ok_ts=now, last_error=None, last_new=new, count=len(items))
            if new:
                self.store.add_poll({"k": "poll", "ts": round(now, 1), "ok": True, "n": len(items), "new": new})
        except Exception as exc:                   # noqa: BLE001
            self.status["last_error"] = f"{type(exc).__name__}: {exc}"
            self.store.add_poll({"k": "poll", "ts": round(now, 1), "ok": False, "error": self.status["last_error"]})
        return dict(self.status)

    def mark_reviewed(self, key: str, on: bool = True) -> bool:
        return self.store.set_review(key, on)

    # --- vista
    def _memory(self, now: float) -> list:
        if self.memory_path and now - self._mem_ts > 60:
            self._mem_ts = now
            self._mem_events = load_memory_events(self.memory_path)
        return self._mem_events

    def view(self, live_events: Iterable[dict], dealers_data: Optional[dict] = None, clock: Optional[dict] = None, catalog: Optional[dict] = None,
             seen_live: Optional[dict] = None, now: Optional[float] = None) -> dict:
        now = time.time() if now is None else now
        dealers = sorted((dealers_data or {}).keys())
        merged = {}
        for e in list(self._memory(now)) + list(live_events or []):
            if e.get("id") is not None and e.get("type") in INTERESTING:
                merged[e["id"]] = e
        events = [_event_summary(e) for e in sorted(merged.values(), key=lambda x: (x.get("tick") or 0, x.get("id") or 0))]
        first_tick = events[0]["tick"] if events else None
        mon = {"dealers": set(dealers), "sets": set()}
        rows, alerts = [], []
        for key, m in sorted(self.store.msgs.items(), key=lambda kv: ((kv[1].get("tick") or 0), kv[1].get("id") or 0)):
            full = dict(m, inferred=classify(m, dealers, catalog))
            tl = correlate(full, events, (clock or {}).get("tick"), first_tick, seen_live)
            ver = verification(full, dealers_data, tl, events)
            al = alerts_for(full, mon)
            reviewed = self.store.reviews.get(key, False)
            sights = self.store.sightings.get(key, {})
            rows.append({"key": key, "id": m["id"], "tick": m.get("tick"), "at_hours": m.get("at_hours"), "source": m.get("source"),
                         "source_name": m.get("source_name") or SOURCES.get(m.get("source")), "headline": m.get("headline"), "body": m.get("body"),
                         "text": m.get("text"), "captured_ts": m.get("captured_ts"), "first_seen_tick": m.get("first_seen_tick"),
                         "provenance": m.get("provenance"), "event_id": m.get("event_id") or next((s.get("event_id") for s in sights.values()
                                                                                                if s.get("event_id")), None),
                         "also_seen_via": sorted(sights), "inferred": full["inferred"], "verification": ver, "timeline": tl,
                         "reviewed": reviewed, "alerts": [a["key"] for a in al], "status": "revisado" if reviewed else "pendiente"})
            for a in al:
                alerts.append(dict(a, reviewed=reviewed, tick=m.get("tick"), headline=m.get("headline")))
        st = self.status
        fresh = None if not st.get("last_ok_ts") else round(now - st["last_ok_ts"], 1)
        return {"messages": rows, "alerts": alerts, "n_unreviewed": sum(1 for r in rows if not r["reviewed"]),
                "ingestion": {"source": "GET /api/news (público, sin clave) + news.posted del feed", "interval_s": self.poll_every,
                              "last_ok_age_s": fresh, "last_error": st.get("last_error"), "polls": st.get("polls"), "stored": len(rows),
                              "history": "la API devuelve todo el historial disponible; el feed solo corrobora (cobertura parcial)",
                              "events_for_correlation": len(events), "events_first_tick": first_tick},
                "limits": ["el feed publica hilos y ofertas estructuradas, no el texto de las conversaciones (text=null)",
                           "no hay datos de quién escuchó cada mensaje", "una correlación temporal no prueba causalidad"]}
