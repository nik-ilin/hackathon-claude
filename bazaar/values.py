"""Private valuation: what a card, a pack or a whole trade is worth to US.

Ground truth is the server: `your_value` on each held asset and `/api/me/value?card=`
for one more copy. On top of that we model the page bonus, verified on our complete
La Latina page: each page card's `your_value` shows base + the whole page bonus
(0.25 × Σ base page values = 86.1), i.e. what we would lose by removing it.
"""
from __future__ import annotations

import collections
import math
from typing import Optional

PAGE_RARITIES = ("common", "uncommon", "rare")
PAGE_BONUS_FACTOR = 0.25         # bonus of a complete page = 25 % of Σ base page values (LAT: 86.1 on 344.5)
PROTECTED = math.inf             # loss of a card we must never give away


class ValueBook:
    """Built from one `me()` + `catalog()` snapshot; caches `value()` per (ref, copies held)."""

    def __init__(self, api, me: dict, catalog: dict, cache: dict = None):
        self.api, self.me, self.cat = api, me, catalog
        self.affinity: dict = me.get("affinity") or {}
        self.marginals: list = catalog["values"]["copy_marginals"]
        self.book = {k: v["book"] for k, v in catalog["rarities"].items()}
        self.cards = {c["id"]: {**c, "set": s["id"]} for s in catalog["sets"] for c in s["cards"]}
        self.held = collections.Counter(a["ref"] for a in me["assets"] if a.get("kind") == "card")
        self.pages = {p["set"]: p for p in (me.get("album") or {}).get("pages", [])}
        self._cache: dict = cache if cache is not None else {}
        # the copy we always keep of each ref: lowest serial (collectors' favourite)
        self.keeper: dict = {}
        for a in sorted((a for a in me["assets"] if a.get("kind") == "card"), key=lambda a: a.get("serial", 10**6)):
            self.keeper.setdefault(a["ref"], a["id"])

    # ── primitives ──────────────────────────────────────────────────────
    def base(self, ref: str) -> float:
        c = self.cards[ref]
        return self.book[c["rarity"]] * self.affinity.get(c["set"], 1.0)

    def page_base_sum(self, set_id: str) -> float:
        return sum(self.base(r) for r, c in self.cards.items()
                   if c["set"] == set_id and c["rarity"] in PAGE_RARITIES)

    def page_bonus(self, set_id: str) -> float:
        return PAGE_BONUS_FACTOR * self.page_base_sum(set_id)

    def page_state(self, set_id: str) -> tuple[int, int, bool]:
        p = self.pages.get(set_id)
        if p:
            return p["have"], p["of"], bool(p["complete"])
        of = sum(1 for c in self.cards.values() if c["set"] == set_id and c["rarity"] in PAGE_RARITIES)
        have = sum(1 for r, c in self.cards.items()
                   if c["set"] == set_id and c["rarity"] in PAGE_RARITIES and self.held[r])
        return have, of, have >= of

    def value_next(self, ref: str) -> Optional[float]:
        """Server's value of one more copy (None if the server could not tell us)."""
        c = self.cards.get(ref, {})
        key = (ref, self.held[ref], self.page_state(c["set"])[0] if c else 0)
        if key not in self._cache:
            try:
                self._cache[key] = float(self.api.value(ref)["your_value"])
            except Exception:
                self._cache[key] = None
        return self._cache[key]

    # ── decisions ───────────────────────────────────────────────────────
    def gain(self, refs: list) -> Optional[float]:
        """Value of receiving these cards (handles several copies and page completion)."""
        total, extra = 0.0, collections.Counter()
        completes: set = set()
        for ref in refs:
            if ref not in self.cards:
                return None
            n = self.held[ref] + extra[ref]
            if extra[ref] == 0:
                v = self.value_next(ref)
                if v is None:
                    return None
            else:
                v = self.base(ref) * (self.marginals[n] if n < len(self.marginals) else 0)
            total += v
            extra[ref] += 1
        # page completion: every missing page card supplied ⇒ the whole bonus appears
        for set_id in {self.cards[r]["set"] for r in refs}:
            have, of, complete = self.page_state(set_id)
            if complete:
                continue
            new = {r for r in extra if self.cards[r]["set"] == set_id
                   and self.cards[r]["rarity"] in PAGE_RARITIES and self.held[r] == 0}
            if have + len(new) >= of and all(
                    (self.value_next(r) or 0) < self.base(r) + 0.5 * self.page_bonus(set_id) for r in new):
                completes.add(set_id)             # server value doesn't already include the bonus
        return total + sum(self.page_bonus(s) for s in completes)

    def loss(self, asset: dict) -> float:
        """Value we give up by handing over this exact asset."""
        if asset.get("kind") == "pack":
            return self.pack_ev(asset["ref"])
        ref = asset["ref"]
        c = self.cards.get(ref)
        if c is None:
            return PROTECTED
        yv = float(asset.get("your_value") or self.base(ref))
        if self.held[ref] >= 2 and self.keeper.get(ref) != asset.get("id"):
            return yv                                   # a spare copy: its own marginal
        if self.held[ref] >= 2:                         # the keeper of several copies: first-copy value
            yv = self.base(ref) * self.marginals[0]
            if c["rarity"] in PAGE_RARITIES and self.page_state(c["set"])[2]:
                return PROTECTED
        if c["rarity"] not in PAGE_RARITIES:
            return yv
        have, of, complete = self.page_state(c["set"])
        if complete:
            return max(yv, self.base(ref) + self.page_bonus(c["set"]))   # breaks the page: lose the bonus
        # option value of finishing the page later, growing as we get closer
        return yv + 0.5 * self.page_bonus(c["set"]) * (have / of) ** 3

    def pack_ev(self, pack_id: str) -> float:
        pack = next((p for p in self.cat["packs"] if p["id"] == pack_id), None)
        if not pack:
            return 0.0
        released = {s for s in self.affinity if any(self.cards[r]["set"] == s for r in self.held)} \
            | set(self.pages)
        def ev_rarity(rarity: str) -> float:
            vals = []
            for ref, c in self.cards.items():
                if c["rarity"] != rarity or c["set"] not in released:
                    continue
                n = self.held[ref]
                vals.append(self.base(ref) * (self.marginals[n] if n < len(self.marginals) else 0))
            return sum(vals) / len(vals) if vals else 0.0
        return sum(sum(p * ev_rarity(r) for r, p in slot.items()) for slot in pack["slots"])

    def trade_delta(self, give_assets: list, give_cash: int, get_refs: list, get_cash: int,
                    fee: int = 0) -> Optional[float]:
        """Δ private value of a trade (positive = we gain). None ⇒ unknown ⇒ caller must block."""
        g = self.gain(get_refs) if get_refs else 0.0
        if g is None:
            return None
        lost = sum(self.loss(a) for a in give_assets)
        return g - lost + get_cash - give_cash - fee
