"""MARKET / COUNTERPARTY INTELLIGENCE persistente (SQLite, `data/market.db`). NO ejecuta operaciones ni llama a la API.

    snapshot() del coordinador → Intelligence.ingest(snapshot) → update_models() → señales que consume el coordinador

Responsabilidades (sin solaparse con el resto):
- valor privado → trading.Valuation · valor de mercado → market_intel · seguridad de páginas → page_guard
- AQUÍ: creencias sobre contrapartes. Qué ha publicado cada equipo (evidencia de posesión), qué pide (interés por
  carta y por colección), cotas de su precio de reserva, perfil de estrategia y compatibilidad para negociar.

Reglas de inferencia:
- El feed y los tablones solo muestran lo PUBLICADO. Que no hayamos visto una carta a un equipo no prueba que no la
  tenga: P_owns sin evidencia = 0 con confianza UNKNOWN (no "no la tiene").
- Una puja de 82 P prueba reserva ≥ 82 P, no su máximo: el techo queda en None salvo evidencia directa.
- Toda creencia lleva confianza (UNKNOWN/LOW/MEDIUM/HIGH), evidencia y decaimiento por antigüedad.
- Un fallo de la base de datos nunca detiene al coordinador (degradación con aviso).
Biblioteca estándar.
"""
from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

DEFAULT_DB = Path(__file__).resolve().parent / "data" / "market.db"
HALF_LIFE = 120          # ticks: una evidencia de hace 120 ticks pesa la mitad
WINDOW = 720             # ticks de historia que entran en los modelos
OWN_W = {"ask": 0.85, "swap_give": 0.85, "directed_give": 0.9, "settled_in": 0.95}
STRATEGIES = ("PAGE_COMPLETION", "RARE_ACCUMULATION", "CASH_ACCUMULATION", "LIQUIDATION", "ARBITRAGE",
              "MARKET_MAKING", "BROAD_COLLECTION", "UNKNOWN")
RARE = {"rare", "epic", "legendary"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS teams (team_id TEXT PRIMARY KEY, name TEXT, first_seen INTEGER, last_seen INTEGER,
    venue_owner TEXT);
CREATE TABLE IF NOT EXISTS cards (ref TEXT PRIMARY KEY, set_id TEXT, rarity TEXT, book REAL, page INTEGER);
CREATE TABLE IF NOT EXISTS venues (venue TEXT PRIMARY KEY, name TEXT, owner TEXT, fee_bps INTEGER,
    fee_per_card INTEGER, mechanism TEXT, last_seen INTEGER);
CREATE TABLE IF NOT EXISTS offers (offer_id INTEGER PRIMARY KEY, maker TEXT, to_team TEXT, venue TEXT,
    thread INTEGER, give_json TEXT, want_json TEXT, kind TEXT, ref TEXT, want_ref TEXT, price INTEGER,
    created_tick INTEGER, expires_tick INTEGER, first_seen INTEGER, last_seen INTEGER, status TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS offer_snapshots (offer_id INTEGER, tick INTEGER, status TEXT, price INTEGER,
    PRIMARY KEY (offer_id, tick));
CREATE TABLE IF NOT EXISTS settlements (settlement_id INTEGER PRIMARY KEY, tick INTEGER, venue TEXT, kind TEXT,
    parties_json TEXT, items_json TEXT, price INTEGER, fee INTEGER, persona TEXT);
CREATE TABLE IF NOT EXISTS market_snapshots (tick INTEGER, ref TEXT, venue TEXT, best_bid INTEGER,
    best_ask INTEGER, n_bids INTEGER, n_asks INTEGER, PRIMARY KEY (tick, ref, venue));
CREATE TABLE IF NOT EXISTS inventory_evidence (team_id TEXT, ref TEXT, tick INTEGER, kind TEXT, weight REAL,
    source_id TEXT, PRIMARY KEY (team_id, ref, kind, source_id));
CREATE TABLE IF NOT EXISTS team_card_interest (team_id TEXT, ref TEXT, score REAL, p REAL, n_signals INTEGER,
    last_tick INTEGER, evidence_json TEXT, PRIMARY KEY (team_id, ref));
CREATE TABLE IF NOT EXISTS team_set_interest (team_id TEXT, set_id TEXT, score REAL, p REAL, n_cards INTEGER,
    last_tick INTEGER, PRIMARY KEY (team_id, set_id));
CREATE TABLE IF NOT EXISTS counterparty_profiles (team_id TEXT PRIMARY KEY, dominant TEXT, probs_json TEXT,
    confidence TEXT, evidence_json TEXT, n_signals INTEGER, updated_tick INTEGER);
CREATE TABLE IF NOT EXISTS reservation_estimates (team_id TEXT, ref TEXT, side TEXT, lower_bound REAL,
    median_estimate REAL, upper_bound REAL, confidence TEXT, evidence_json TEXT, updated_tick INTEGER,
    PRIMARY KEY (team_id, ref, side));
CREATE TABLE IF NOT EXISTS interactions (tick INTEGER, team_id TEXT, kind TEXT, ref TEXT, price INTEGER,
    offer_id INTEGER, detail_json TEXT, PRIMARY KEY (kind, offer_id));
CREATE TABLE IF NOT EXISTS dealer_interactions (offer_id INTEGER PRIMARY KEY, tick INTEGER, team_id TEXT,
    dealer TEXT, thread INTEGER, ref TEXT, price INTEGER, side TEXT);
CREATE TABLE IF NOT EXISTS model_metadata (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS ix_offers_maker ON offers (maker);
CREATE INDEX IF NOT EXISTS ix_offers_ref ON offers (ref);
CREATE INDEX IF NOT EXISTS ix_offers_want ON offers (want_ref);
CREATE INDEX IF NOT EXISTS ix_offers_venue ON offers (venue);
CREATE INDEX IF NOT EXISTS ix_offers_tick ON offers (last_seen);
CREATE INDEX IF NOT EXISTS ix_snap_tick ON offer_snapshots (tick);
CREATE INDEX IF NOT EXISTS ix_settle_tick ON settlements (tick);
CREATE INDEX IF NOT EXISTS ix_ev_team ON inventory_evidence (team_id, ref);
CREATE INDEX IF NOT EXISTS ix_ev_ref ON inventory_evidence (ref);
CREATE INDEX IF NOT EXISTS ix_int_team ON interactions (team_id, ref);
CREATE INDEX IF NOT EXISTS ix_mkt_ref ON market_snapshots (ref, venue);
"""


def confidence_label(n: int) -> str:
    return "UNKNOWN" if n <= 0 else "LOW" if n < 5 else "MEDIUM" if n < 15 else "HIGH"


def decay(age: float) -> float:
    return 0.5 ** (max(0.0, age) / HALF_LIFE)


def _wanted(side: dict) -> list:
    return [t[5:] for t in list(side.get("types") or []) + [f"card:{c}" for c in side.get("cards") or []]
            if isinstance(t, str) and t.startswith("card:")]


def _given(side: dict) -> list:
    return [(a.get("id"), a.get("ref")) for a in side.get("assets") or [] if isinstance(a, dict) and a.get("ref")]


def classify_offer(o: dict) -> tuple:
    """(kind, ref ofrecida, ref pedida, precio). kind: ask | bid | swap | bundle | other."""
    g, w = o.get("give") or {}, o.get("want") or {}
    gv, wt = _given(g), _wanted(w)
    gcash, wcash = int(g.get("cash") or 0), int(w.get("cash") or 0)
    if len(gv) == 1 and not wt and wcash and not gcash:
        return "ask", gv[0][1], None, wcash
    if gcash and not gv and len(wt) == 1 and not wcash:
        return "bid", None, wt[0], gcash
    if len(gv) == 1 and len(wt) == 1 and not gcash and not wcash:
        return "swap", gv[0][1], wt[0], 0
    if gv or wt:
        return "bundle", gv[0][1] if gv else None, wt[0] if wt else None, max(gcash, wcash)
    return "other", None, None, max(gcash, wcash)


@dataclass
class ReservationEstimate:
    team: str
    ref: str
    side: str                       # buy = lo que pagaría · sell = a cuánto vendería
    lower_bound: Optional[float]
    median_estimate: Optional[float]
    upper_bound: Optional[float]
    confidence: str
    evidence: list = field(default_factory=list)

    def interval(self) -> list:
        return [self.lower_bound, self.upper_bound]


@dataclass
class CounterpartyContext:
    team: str
    likely_wants: list
    likely_owns: list
    reservation_estimates: list
    previous_offers: list
    previous_settlements: list
    response_speed_ticks: Optional[float]
    concession_behaviour: str
    preferred_venues: list
    profile: dict

    def as_dict(self) -> dict:
        return asdict(self)


class Intelligence:
    def __init__(self, path=DEFAULT_DB, team: Optional[str] = None):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.team = team
        self.tick = int(self.meta("last_tick") or 0)

    def close(self):
        self.db.close()

    # ------------------------------------------------------------------ metadatos
    def meta(self, key, value=None):
        if value is None:
            r = self.db.execute("SELECT value FROM model_metadata WHERE key=?", (key,)).fetchone()
            return r["value"] if r else None
        self.db.execute("INSERT INTO model_metadata(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value="
                        "excluded.value", (key, str(value)))

    # ------------------------------------------------------------------ ingesta (reutiliza la instantánea)
    def ingest(self, snap: dict) -> dict:
        """Idempotente: repetir la misma instantánea no duplica filas (claves primarias + UPSERT)."""
        tick = int((snap.get("clock") or {}).get("tick") or 0)
        me = snap.get("me") or {}
        self.team = self.team or me.get("id")
        self.tick = tick
        n = {"offers": 0, "settlements": 0, "evidence": 0}
        with self.db:
            if self.meta("catalog_cards") != str(sum(len(s.get("cards", [])) for s in (snap.get("catalog") or {}).get("sets", []))):
                for s in (snap.get("catalog") or {}).get("sets", []):
                    for c in s.get("cards", []):
                        self.db.execute("INSERT INTO cards VALUES(?,?,?,?,?) ON CONFLICT(ref) DO UPDATE SET set_id="
                                        "excluded.set_id, rarity=excluded.rarity, book=excluded.book, page=excluded.page",
                                        (c["id"], s["id"], c.get("rarity"), c.get("book"), int(bool(c.get("page")))))
                self.meta("catalog_cards", sum(len(s.get("cards", [])) for s in (snap.get("catalog") or {}).get("sets", [])))
            for v in (snap.get("venues") or {}).get("venues", []):
                self.db.execute("INSERT INTO venues VALUES(?,?,?,?,?,?,?) ON CONFLICT(venue) DO UPDATE SET name="
                                "excluded.name, owner=excluded.owner, fee_bps=excluded.fee_bps, fee_per_card="
                                "excluded.fee_per_card, mechanism=excluded.mechanism, last_seen=excluded.last_seen",
                                (v.get("venue"), v.get("name"), v.get("owner"), v.get("fee_bps"), v.get("fee_per_card"),
                                 (v.get("rules") or {}).get("mechanism", "posted"), tick))
                if v.get("owner") and str(v["owner"]).startswith("t"):
                    self._team(v["owner"], tick, venue=v.get("venue"))
            pool = []
            for b in (snap.get("boards") or {}).values():
                pool += [(o, "board") for o in (b or {}).get("offers", [])]
            pool += [(o, "board") for o in (snap.get("board") or {}).get("offers", [])]
            pool += [(o, "mine") for o in (snap.get("offers") or {}).get("offers", [])]
            for t in ((snap.get("threads") or {}).get("open") or []) + ((snap.get("threads") or {}).get("deal") or []):
                pool += [(o, "thread") for o in t.get("standing_offers") or []]
            events = (snap.get("feed") or {}).get("events", [])
            for e in events:
                p = e.get("payload") or {}
                if e.get("type") == "offer.listed" and isinstance(p.get("offer"), dict):
                    pool.append((p["offer"], "feed"))
                elif e.get("type") == "thread.message" and isinstance(p.get("offer"), dict) and p.get("kind") == "persona":
                    self._dealer(p, int(e.get("tick") or tick))
            seen = set()
            for o, src in pool:
                if not isinstance(o, dict) or o.get("id") is None or o["id"] in seen:
                    continue
                seen.add(o["id"])
                n["offers"] += self._offer(o, tick, src)
                n["evidence"] += self._evidence_from_offer(o, tick)
            for e in events:
                p = e.get("payload") or {}
                if e.get("type") == "offer.cancelled" and p.get("offer") is not None:
                    self.db.execute("UPDATE offers SET status='cancelled', last_seen=MAX(last_seen, ?) WHERE offer_id=?"
                                    " AND status='open'", (int(e.get("tick") or tick), p["offer"]))
                elif e.get("type") == "settlement" and p.get("settlement") is not None:
                    n["settlements"] += self._settlement(p, int(e.get("tick") or p.get("tick") or tick))
            self._market_snapshot(snap, tick)
            self.meta("last_tick", tick)
        return n

    def _team(self, team, tick, venue=None):
        if not team or not str(team).startswith("t"):
            return
        self.db.execute("INSERT INTO teams(team_id, first_seen, last_seen, venue_owner) VALUES(?,?,?,?) ON CONFLICT"
                        "(team_id) DO UPDATE SET last_seen=MAX(last_seen, excluded.last_seen), venue_owner="
                        "COALESCE(excluded.venue_owner, venue_owner)", (team, tick, tick, venue))

    def _offer(self, o, tick, src) -> int:
        kind, ref, want_ref, price = classify_offer(o)
        maker = o.get("maker")
        self._team(maker, tick)
        new = self.db.execute("SELECT 1 FROM offers WHERE offer_id=?", (o["id"],)).fetchone() is None
        self.db.execute(
            "INSERT INTO offers VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(offer_id) DO UPDATE SET "
            "last_seen=MAX(last_seen, excluded.last_seen), status=excluded.status, expires_tick=excluded.expires_tick",
            (o["id"], maker, o.get("to"), o.get("venue"), o.get("thread"), json.dumps(o.get("give") or {}),
             json.dumps(o.get("want") or {}), kind, ref, want_ref, price, o.get("created_tick"), o.get("expires_tick"),
             tick, tick, o.get("status") or "open", src))
        self.db.execute("INSERT OR IGNORE INTO offer_snapshots VALUES(?,?,?,?)", (o["id"], tick, o.get("status"), price))
        if maker and str(maker).startswith("t") and (want_ref or ref):
            self.db.execute("INSERT OR IGNORE INTO interactions VALUES(?,?,?,?,?,?,?)",
                            (o.get("created_tick") or tick, maker, f"offer:{kind}", want_ref or ref, price, o["id"],
                             json.dumps({"to": o.get("to"), "venue": o.get("venue")})))
        return int(new)

    def _evidence_from_offer(self, o, tick) -> int:
        maker = o.get("maker")
        if not maker or not str(maker).startswith("t"):
            return 0
        kind, _, _, _ = classify_offer(o)
        n = 0
        for aid, ref in _given(o.get("give") or {}):
            ev = "directed_give" if o.get("to") else {"ask": "ask", "swap": "swap_give"}.get(kind, "ask")
            cur = self.db.execute("INSERT OR IGNORE INTO inventory_evidence VALUES(?,?,?,?,?,?)",
                                  (maker, ref, o.get("created_tick") or tick, ev, OWN_W[ev], f"offer:{o['id']}"))
            n += cur.rowcount
        return n

    def _settlement(self, p, tick) -> int:
        cur = self.db.execute("INSERT OR IGNORE INTO settlements VALUES(?,?,?,?,?,?,?,?,?)",
                              (p["settlement"], tick, p.get("venue"), p.get("kind"), json.dumps(p.get("parties") or []),
                               json.dumps(p.get("items") or []), p.get("price"), p.get("fee"), p.get("persona")))
        if not cur.rowcount:
            return 0
        for it in p.get("items") or []:
            if it.get("kind") != "card" or not it.get("ref"):
                continue
            if it.get("to") and str(it["to"]).startswith("t"):
                self.db.execute("INSERT OR IGNORE INTO inventory_evidence VALUES(?,?,?,?,?,?)",
                                (it["to"], it["ref"], tick, "settled_in", OWN_W["settled_in"], f"settle:{p['settlement']}"))
                self.db.execute("INSERT OR IGNORE INTO interactions VALUES(?,?,?,?,?,?,?)",
                                (tick, it["to"], "bought", it["ref"], p.get("price"), -int(p["settlement"]), "{}"))
            if it.get("frm") and str(it["frm"]).startswith("t"):
                self.db.execute("INSERT OR IGNORE INTO inventory_evidence VALUES(?,?,?,?,?,?)",
                                (it["frm"], it["ref"], tick, "settled_out", -1.0, f"settle:{p['settlement']}"))
                self.db.execute("INSERT OR IGNORE INTO interactions VALUES(?,?,?,?,?,?,?)",
                                (tick, it["frm"], "sold", it["ref"], p.get("price"), -int(p["settlement"]) - 10 ** 9, "{}"))
        return 1

    def _dealer(self, p, tick):
        o = p["offer"]
        team, dealer = p.get("team"), p.get("with")
        side = "buy" if o.get("maker") == team and (o.get("give") or {}).get("cash") else "sell"
        refs = _wanted(o.get("want") or {}) + _wanted(o.get("give") or {})
        self.db.execute("INSERT OR IGNORE INTO dealer_interactions VALUES(?,?,?,?,?,?,?,?)",
                        (o.get("id"), tick, team, dealer, p.get("thread"), refs[0] if refs else None,
                         int((o.get("give") or {}).get("cash") or (o.get("want") or {}).get("cash") or 0), side))

    def _market_snapshot(self, snap, tick):
        best = {}
        for vid, b in (snap.get("boards") or {}).items():
            for o in (b or {}).get("offers", []):
                if o.get("status") != "open":
                    continue
                kind, ref, want_ref, price = classify_offer(o)
                if kind == "ask":
                    k = (ref, vid)
                    x = best.setdefault(k, [None, None, 0, 0])
                    x[1] = price if x[1] is None else min(x[1], price)
                    x[3] += 1
                elif kind == "bid":
                    k = (want_ref, vid)
                    x = best.setdefault(k, [None, None, 0, 0])
                    x[0] = price if x[0] is None else max(x[0], price)
                    x[2] += 1
        for (ref, vid), (bb, ba, nb, na) in best.items():
            self.db.execute("INSERT OR IGNORE INTO market_snapshots VALUES(?,?,?,?,?,?,?)", (tick, ref, vid, bb, ba, nb, na))

    # ------------------------------------------------------------------ modelos
    def update_models(self, tick: Optional[int] = None) -> dict:
        tick = self.tick if tick is None else tick
        since = tick - WINDOW
        sets = {r["ref"]: (r["set_id"], r["rarity"]) for r in self.db.execute("SELECT ref, set_id, rarity FROM cards")}
        rows = self.db.execute("SELECT * FROM offers WHERE maker LIKE 't%' AND last_seen >= ? ORDER BY offer_id",
                               (since,)).fetchall()
        sold = {}
        for r in self.db.execute("SELECT team_id, ref, tick FROM inventory_evidence WHERE kind='settled_out' AND tick>=?",
                                 (since,)):
            sold.setdefault((r["team_id"], r["ref"]), []).append(r["tick"])
        bought = {}
        for r in self.db.execute("SELECT team_id, ref, tick FROM inventory_evidence WHERE kind='settled_in' AND tick>=?",
                                 (since,)):
            bought.setdefault((r["team_id"], r["ref"]), []).append(r["tick"])
        interest, feats, bids = {}, {}, {}
        for r in rows:
            team = r["maker"]
            f = feats.setdefault(team, {"bid": 0, "ask": 0, "swap": 0, "wanted_sets": {}, "sold_sets": set(),
                                        "rare_wants": 0, "wants": 0, "both_sides": set(), "asks_refs": set(),
                                        "wants_refs": set(), "evidence": []})
            age = tick - (r["last_seen"] or tick)
            persist = min(10, (r["last_seen"] or 0) - (r["first_seen"] or 0))
            if r["want_ref"]:
                w = (1.2 if r["to_team"] else 1.0 if r["kind"] == "bid" else 0.8) * decay(age) * (1 + 0.05 * persist)
                key = (team, r["want_ref"])
                x = interest.setdefault(key, {"score": 0.0, "n": 0, "last": 0, "ev": []})
                x["score"] += w
                x["n"] += 1
                x["last"] = max(x["last"], r["last_seen"] or 0)
                x["ev"].append(f"{r['kind']} #{r['offer_id']} {r['price'] or ''} P t{r['created_tick']}"
                               + (f" → {r['to_team']}" if r["to_team"] else ""))
                if r["kind"] == "bid":
                    bids.setdefault(key, []).append((r["created_tick"] or r["first_seen"], r["price"], r["offer_id"],
                                                     r["to_team"]))
                sid, rar = sets.get(r["want_ref"], (None, None))
                f["wants"] += 1
                f["wants_refs"].add(r["want_ref"])
                f["rare_wants"] += int(rar in RARE)
                if sid:
                    f["wanted_sets"][sid] = f["wanted_sets"].get(sid, 0) + 1
            if r["ref"] and r["kind"] in ("ask", "swap", "bundle"):
                f["asks_refs"].add(r["ref"])
                sid, _ = sets.get(r["ref"], (None, None))
                if sid:
                    f["sold_sets"].add(sid)
            f[r["kind"] if r["kind"] in ("bid", "ask", "swap") else "ask" if r["kind"] == "bundle" else "bid"] += 1
        # escaladas de puja: +0.5 por cada subida de precio de la misma carta
        for key, xs in bids.items():
            xs.sort()
            ups = sum(1 for a, b in zip(xs, xs[1:]) if (b[1] or 0) > (a[1] or 0))
            if ups:
                interest[key]["score"] += 0.5 * ups
                interest[key]["ev"].append(f"{ups} escalada(s) de puja")
        for key, ticks in bought.items():
            x = interest.setdefault(key, {"score": 0.0, "n": 0, "last": 0, "ev": []})
            x["score"] += 0.6 * sum(decay(tick - t) for t in ticks)
            x["n"] += len(ticks)
            x["last"] = max([x["last"]] + ticks)
            x["ev"].append(f"compró {len(ticks)} vez/veces")
        for (team, ref), ticks in sold.items():
            if (team, ref) in interest:
                interest[(team, ref)]["score"] -= 0.7 * len(ticks)
                interest[(team, ref)]["ev"].append(f"vendió {len(ticks)} vez/veces (señal negativa)")
        with self.db:
            self.db.execute("DELETE FROM team_card_interest")
            self.db.execute("DELETE FROM team_set_interest")
            per_set = {}
            for (team, ref), x in interest.items():
                score = max(0.0, x["score"])
                p = round(1 - math.exp(-score / 1.5), 3)
                self.db.execute("INSERT INTO team_card_interest VALUES(?,?,?,?,?,?,?)",
                                (team, ref, round(score, 3), p, x["n"], x["last"], json.dumps(x["ev"][-8:])))
                sid = sets.get(ref, (None, None))[0]
                if sid:
                    y = per_set.setdefault((team, sid), [0.0, set(), 0])
                    y[0] += score
                    y[1].add(ref)
                    y[2] = max(y[2], x["last"])
            for (team, sid), (score, refs, last) in per_set.items():
                feat = feats.get(team)
                if feat and sid in feat["sold_sets"] and not feat["wanted_sets"].get(sid):
                    score *= 0.5  # vende esa colección: interés más débil
                self.db.execute("INSERT INTO team_set_interest VALUES(?,?,?,?,?,?)",
                                (team, sid, round(score, 3), round(1 - math.exp(-score / 2.5), 3), len(refs), last))
            self._reservations(bids, rows, tick)
            self._profiles(feats, tick)
            self.meta("models_tick", tick)
        return {"teams": len(feats), "card_interest": len(interest)}

    def _reservations(self, bids, rows, tick):
        self.db.execute("DELETE FROM reservation_estimates")
        for (team, ref), xs in bids.items():
            prices = [p for _, p, _, _ in sorted(xs) if p]
            if not prices:
                continue
            lower = max(prices)
            ups = [b - a for a, b in zip(prices, prices[1:]) if b > a]
            median = round(lower + sum(ups) / len(ups), 1) if len(ups) >= 2 else None  # sigue escalando: un paso más
            ev = [f"pujas {prices} (la reserva es ≥ {lower} P; el máximo NO se conoce)"]
            if median is not None:
                ev.append(f"escalada media {sum(ups) / len(ups):.1f} P: estimación central {median} P (débil)")
            self.db.execute("INSERT INTO reservation_estimates VALUES(?,?,?,?,?,?,?,?,?)",
                            (team, ref, "buy", lower, median, None, confidence_label(len(prices)), json.dumps(ev), tick))
        asks = {}
        for r in rows:
            if r["kind"] == "ask" and r["ref"] and r["price"]:
                asks.setdefault((r["maker"], r["ref"]), []).append(r["price"])
        for (team, ref), prices in asks.items():
            self.db.execute("INSERT OR REPLACE INTO reservation_estimates VALUES(?,?,?,?,?,?,?,?,?)",
                            (team, ref, "sell", None, None, min(prices), confidence_label(len(prices)),
                             json.dumps([f"pide {sorted(prices)}: vendería a ≤ {min(prices)} P; el mínimo NO se conoce"]),
                             tick))

    def _profiles(self, feats, tick):
        owners = {r["team_id"]: r["venue_owner"] for r in self.db.execute("SELECT team_id, venue_owner FROM teams")}
        self.db.execute("DELETE FROM counterparty_profiles WHERE team_id=?", (self.team,))
        for team, f in feats.items():
            if team == self.team:
                continue  # no somos una contraparte
            n = f["bid"] + f["ask"] + f["swap"]
            wants, sells = f["wants"], f["ask"] + f["swap"]
            top = max(f["wanted_sets"].values()) if f["wanted_sets"] else 0
            top_share = top / wants if wants else 0.0
            sell_share = sells / n if n else 0.0
            both = len(f["asks_refs"] & f["wants_refs"])
            raw = {
                "PAGE_COMPLETION": top_share * min(1.0, wants / 3),
                "RARE_ACCUMULATION": (f["rare_wants"] / wants if wants else 0.0) * min(1.0, wants / 3),
                "CASH_ACCUMULATION": sell_share * 0.8 if f["ask"] > f["swap"] else sell_share * 0.4,
                "LIQUIDATION": sell_share * min(1.0, len(f["sold_sets"]) / 3) * (1.0 if not wants else 0.4),
                "ARBITRAGE": min(1.0, both / 2),
                "MARKET_MAKING": (0.6 if owners.get(team) else 0.0) + (0.3 if f["bid"] and f["ask"] and n >= 6 else 0.0),
                "BROAD_COLLECTION": (1 - top_share) * min(1.0, len(f["wanted_sets"]) / 3) if wants else 0.0,
                "UNKNOWN": 1.0 / (1 + n),
            }
            total = sum(raw.values()) or 1.0
            probs = {k: round(v / total, 3) for k, v in raw.items()}
            dominant = max(probs, key=probs.get)
            conf = confidence_label(n)
            ev = [f"{f['bid']} pujas, {f['ask']} ventas, {f['swap']} trueques",
                  f"colecciones pedidas {f['wanted_sets'] or '—'}", f"vende de {sorted(f['sold_sets']) or '—'}"]
            if owners.get(team):
                ev.append(f"opera el venue {owners[team]}")
            self.db.execute("INSERT OR REPLACE INTO counterparty_profiles VALUES(?,?,?,?,?,?,?)",
                            (team, dominant, json.dumps(probs), conf, json.dumps(ev), n, tick))

    # ------------------------------------------------------------------ consultas
    def p_owns(self, team: str, ref: str) -> dict:
        rows = self.db.execute("SELECT * FROM inventory_evidence WHERE team_id=? AND ref=? ORDER BY tick",
                               (team, ref)).fetchall()
        pos = [r for r in rows if r["weight"] > 0]
        if not pos:
            return {"p": 0.0, "confidence": "UNKNOWN", "evidence": ["sin evidencia publicada (no prueba que no la tenga)"]}
        last_pos = max(r["tick"] for r in pos)
        q = 1.0
        for r in pos:
            q *= 1 - r["weight"] * decay(self.tick - r["tick"])
        p = 1 - q
        outs = [r for r in rows if r["weight"] < 0 and r["tick"] >= last_pos]
        if outs:
            p *= 0.15  # la vendió después de la última evidencia de tenerla
        return {"p": round(p, 3), "confidence": confidence_label(len(rows)),
                "evidence": [f"{r['kind']} t{r['tick']} ({r['source_id']})" for r in rows[-6:]]}

    def p_wants(self, team: str, ref: str) -> dict:
        r = self.db.execute("SELECT * FROM team_card_interest WHERE team_id=? AND ref=?", (team, ref)).fetchone()
        if not r:
            return {"p": 0.0, "confidence": "UNKNOWN", "evidence": []}
        return {"p": r["p"], "score": r["score"], "confidence": confidence_label(r["n_signals"]),
                "evidence": json.loads(r["evidence_json"] or "[]"), "last_tick": r["last_tick"]}

    def set_interest(self, team: str, set_id: Optional[str] = None) -> list:
        q = "SELECT * FROM team_set_interest WHERE team_id=?" + (" AND set_id=?" if set_id else "") + " ORDER BY score DESC"
        return [dict(r) for r in self.db.execute(q, (team, set_id) if set_id else (team,))]

    def reservation(self, team: str, ref: str, side: str = "buy") -> ReservationEstimate:
        r = self.db.execute("SELECT * FROM reservation_estimates WHERE team_id=? AND ref=? AND side=?",
                            (team, ref, side)).fetchone()
        if not r:
            return ReservationEstimate(team, ref, side, None, None, None, "UNKNOWN", ["sin ofertas observadas"])
        return ReservationEstimate(team, ref, side, r["lower_bound"], r["median_estimate"], r["upper_bound"],
                                   r["confidence"], json.loads(r["evidence_json"] or "[]"))

    def profile(self, team: str) -> dict:
        r = self.db.execute("SELECT * FROM counterparty_profiles WHERE team_id=?", (team,)).fetchone()
        if not r:
            return {"team": team, "dominant_strategy": "UNKNOWN", "probabilities": {}, "confidence": "UNKNOWN",
                    "evidence": ["sin actividad observada"]}
        return {"team": team, "dominant_strategy": r["dominant"], "probabilities": json.loads(r["probs_json"]),
                "confidence": r["confidence"], "evidence": json.loads(r["evidence_json"]), "signals": r["n_signals"]}

    def counterparty_value(self, ref: str, team: str, market_value: Optional[float]) -> dict:
        """Valor ESPERADO de `ref` para `team` (NO es su valor privado real): cota inferior por sus pujas, mercado e
        interés observado. Sin evidencia de interés, se queda en el valor de mercado."""
        res, want = self.reservation(team, ref), self.p_wants(team, ref)
        base = max(x for x in (market_value or 0, res.lower_bound or 0))
        central = res.median_estimate or base
        return {"team": team, "ref": ref, "expected_value": round(central, 1) if central else None,
                "lower_bound": res.lower_bound, "upper_bound": res.upper_bound, "p_wants": want["p"],
                "confidence": res.confidence if res.lower_bound else want["confidence"],
                "basis": "puja observada (cota inferior)" if res.lower_bound else "valor de mercado (sin evidencia propia)",
                "note": "creencia sobre la contraparte, no su valor privado"}

    def trade_compatibility(self, team: str, target: str, our_tradables: list = ()) -> dict:
        own = self.p_owns(team, target)
        wants = [(r, self.p_wants(team, r)["p"]) for r in our_tradables]
        match = max((p for _, p in wants), default=0.0)
        hist = self.db.execute("SELECT count(*) FROM interactions WHERE team_id=? AND kind LIKE 'offer:%' AND "
                               "detail_json LIKE ?", (team, f'%"to": "{self.team}"%')).fetchone()[0]
        response = min(1.0, hist / 3)
        sell = self.reservation(team, target, "sell")
        score = round(0.5 * own["p"] + 0.3 * match + 0.2 * response, 3)
        return {"team": team, "score": score, "ownership_confidence": own["p"], "interest_match": round(match, 3),
                "reservation": sell.interval(), "confidence": own["confidence"],
                "wants_of_ours": [r for r, p in sorted(wants, key=lambda x: -x[1]) if p > 0][:5],
                "directed_to_us": hist}

    def best_counterparties(self, ref: str, our_tradables: list = (), limit: int = 5) -> list:
        """Contra quién negociar para CONSEGUIR `ref` (quién la tiene), ordenado por compatibilidad."""
        teams = [r["team_id"] for r in self.db.execute(
            "SELECT DISTINCT team_id FROM inventory_evidence WHERE ref=? AND weight>0", (ref,))
            if r["team_id"] != self.team]
        out = [self.trade_compatibility(t, ref, our_tradables) for t in teams]
        return sorted([x for x in out if x["ownership_confidence"] > 0], key=lambda x: -x["score"])[:limit]

    def best_buyers(self, ref: str, limit: int = 5) -> list:
        """Quién QUIERE `ref` (para vender un duplicado): interés, cota de reserva y si nos lo ha dirigido."""
        out = []
        for r in self.db.execute("SELECT * FROM team_card_interest WHERE ref=? ORDER BY score DESC", (ref,)):
            if r["team_id"] == self.team:
                continue
            res = self.reservation(r["team_id"], ref)
            direct = self.db.execute("SELECT max(price) FROM offers WHERE maker=? AND want_ref=? AND to_team=? AND "
                                     "kind='bid'", (r["team_id"], ref, self.team)).fetchone()[0]
            out.append({"team": r["team_id"], "p_wants": r["p"], "interest": interest_label(r["p"]),
                        "reservation": [res.lower_bound, res.upper_bound], "median": res.median_estimate,
                        "confidence": res.confidence, "directed_bid_to_us": direct,
                        "evidence": json.loads(r["evidence_json"] or "[]")[-4:]})
        return out[:limit]

    def context(self, team: str) -> CounterpartyContext:
        wants = [dict(ref=r["ref"], p=r["p"]) for r in self.db.execute(
            "SELECT ref, p FROM team_card_interest WHERE team_id=? ORDER BY score DESC LIMIT 8", (team,))]
        owns = sorted({r["ref"] for r in self.db.execute(
            "SELECT ref FROM inventory_evidence WHERE team_id=? AND weight>0", (team,))})
        owns = [dict(ref=x, **{k: v for k, v in self.p_owns(team, x).items() if k != "evidence"}) for x in owns][:12]
        res = [asdict(self.reservation(team, r["ref"])) for r in self.db.execute(
            "SELECT ref FROM reservation_estimates WHERE team_id=? AND side='buy'", (team,))][:8]
        offers = [dict(r) for r in self.db.execute(
            "SELECT offer_id, kind, ref, want_ref, price, to_team, venue, created_tick, status FROM offers WHERE "
            "maker=? ORDER BY offer_id DESC LIMIT 10", (team,))]
        mine = self.db.execute("SELECT created_tick FROM offers WHERE maker=? AND to_team=? ORDER BY created_tick",
                               (self.team, team)).fetchall()
        theirs = self.db.execute("SELECT created_tick FROM offers WHERE maker=? AND to_team=? ORDER BY created_tick",
                                 (team, self.team)).fetchall()
        gaps = [t["created_tick"] - m["created_tick"] for m in mine for t in theirs[:1]
                if t["created_tick"] and m["created_tick"] and t["created_tick"] >= m["created_tick"]]
        prices = [o["price"] for o in reversed(offers) if o["kind"] == "bid" and o["price"]]
        conc = ("sube sus pujas" if len(prices) >= 2 and prices[-1] > prices[0] else
                "pujas estables" if len(prices) >= 2 else "sin historial suficiente")
        venues = [r["venue"] for r in self.db.execute(
            "SELECT venue, count(*) n FROM offers WHERE maker=? AND venue IS NOT NULL GROUP BY venue ORDER BY n DESC "
            "LIMIT 3", (team,))]
        settles = [dict(tick=r["tick"], price=r["price"]) for r in self.db.execute(
            "SELECT tick, price, parties_json FROM settlements ORDER BY tick DESC LIMIT 400")
            if team in json.loads(r["parties_json"] or "[]") and self.team in json.loads(r["parties_json"] or "[]")][:5]
        return CounterpartyContext(team, wants, owns, res, offers, settles, min(gaps) if gaps else None, conc, venues,
                                   self.profile(team))

    def stats(self) -> dict:
        tables = [r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {t: self.db.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables} | \
            {"last_tick": self.meta("last_tick"), "models_tick": self.meta("models_tick"), "path": self.path}


def interest_label(p: float) -> str:
    return "HIGH" if p >= 0.7 else "MEDIUM" if p >= 0.4 else "LOW" if p > 0 else "UNKNOWN"
