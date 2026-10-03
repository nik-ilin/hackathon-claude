"""Explica la política vigente de duelos como decisiones para un agente.

Las acciones salen de `duels.duel_candidates`, calibrado con los 24 duelos
propios de práctica. El feed público aporta cierres agregados; no revela el
límite del rival ni justifica cambiar una acción individual.
"""
from __future__ import annotations

from collections import Counter
from typing import Iterable

import duels


def feed_evidence(events: Iterable[dict]) -> dict:
    seen = set()
    statuses: Counter = Counter()
    sessions: Counter = Counter()
    for event in events:
        if not isinstance(event, dict) or event.get("type") != "duel.closed":
            continue
        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        identity = event.get("id")
        if identity is not None:
            if identity in seen:
                continue
            seen.add(identity)
        statuses[payload.get("status") or "unknown"] += 1
        sessions[str(payload.get("session") or "unknown")] += 1
    n = sum(statuses.values())
    return {"closed": n, "no_deal": statuses["no_deal"],
            "no_deal_rate": statuses["no_deal"] / n if n else None,
            "sessions": dict(sessions), "basis": "public_closures_only",
            "confidence": "small_sample" if n < 8 else "descriptive_only"}


def _waiting_reason(duel: dict, tick: int) -> tuple[str, list[str]]:
    if duel.get("deadline_tick") is None:
        return "missing_deadline", ["snapshot_incomplete", "no_action"]
    if duel["deadline_tick"] <= tick:
        return "expired_snapshot", ["deadline_reached", "no_action"]
    if "days" in (duel.get("issues") or []) and not duels.PARAMS["PLAY_DAYS"]:
        return "days_utility_unverified", ["two_issue", "no_action"]
    offer = duel.get("rival_offer") or {}
    price = offer.get("price")
    if price is None:
        return "waiting_for_rival_or_opening_window", ["no_rival_offer", "wait"]
    margin = duels.margin(duel, price, offer.get("days"))
    if margin is not None and margin < 0:
        return "rival_offer_outside_limit", ["outside_own_limit", "wait_or_offer_near_deadline"]
    return "rival_may_improve_without_our_message", ["inside_own_limit", "wait_for_better_offer"]


def decision_facts(duel: dict, tick: int, same_deadline: int = 1) -> dict:
    """Números visibles detrás de la ruta (los MISMOS que usa la política: duels.analyze); no cambia la política."""
    deadline = duel.get("deadline_tick")
    facts = {"tick": tick, "deadline_tick": deadline,
             "ticks_left": deadline - tick if isinstance(deadline, (int, float)) else None,
             "issues": duel.get("issues") or ["price"],
             "our_offer_exists": duel.get("your_offer") is not None}
    if duel.get("role") not in ("buyer", "seller") or duel.get("your_limit") is None or deadline is None:
        return facts
    f = duels.analyze(duel, tick, same_deadline)
    action, reason = duels.decide(duel, f)
    facts.update(f)
    facts.update(margin=f["surplus_now"], improvement_per_tick=f["recent_improvement_rate"],
                 speak_at_ticks=duels.PARAMS["SPEAK_AT"], safe_ticks=f["effective_safe_ticks"],
                 policy_action=action, reason=reason, deprecated=duels.DEPRECATED)
    return facts


def _trigger(action: str, facts: dict) -> str:
    if action == "offer":
        return "rival_stalled_counter" if facts.get("policy_action") == "counter" else "opening_window"
    if action == "defer":
        return "acceptance_quota_used"
    if action == "already_sent":
        return "offer_already_sent_this_tick"
    if action != "accept":
        return "wait"
    left = facts.get("ticks_left")
    if left is not None and left <= facts.get("effective_safe_ticks", -1):
        return "deadline_safety"
    if facts.get("phase") == "LATE":
        return "late_phase_positive_deal"
    if facts.get("trend") == "WORSENING":
        return "rival_offer_worsened"
    if facts.get("trend") == "UNKNOWN":
        return "exceptional_first_offer"
    if facts.get("stalled"):
        return "rival_stalled"
    return "surplus_dominates_wait"


def plan(live: list[dict], tick: int, events: Iterable[dict] = (),
         actions: Iterable[dict] = ()) -> list[dict]:
    """Una decisión JSON por duelo, con máximo una aceptación ejecutable.

    Las acciones y el orden son exactamente los de la política actual. `wait`
    y `defer` se vuelven explícitos para el agente; nunca se envían al API.
    """
    evidence = feed_evidence(events)
    candidates = duels.duel_candidates(live, tick)
    same_deadline = Counter(d.get("deadline_tick") for d in live
                            if isinstance(d, dict) and d.get("status") == "live")
    by_id = {d.get("duel"): d for d in live if isinstance(d, dict)}
    prior = [a for a in actions if isinstance(a, dict) and a.get("tick") == tick]
    consumed = lambda a: a.get("sent") is True or a.get("error") == "wait_for_tick"
    accepted = any(a.get("type") == "duel_accept" and consumed(a) for a in prior)
    offered = {a.get("duel") for a in prior
               if a.get("type") == "duel_say" and consumed(a)}
    chosen = set()
    out = []
    for candidate in candidates:
        duel_id = candidate["duel"]
        if duel_id in chosen:
            continue
        chosen.add(duel_id)
        kind = candidate["type"]
        action = "accept" if kind == "duel_accept" else "offer"
        path = ["live_duel", "practice_trajectory_policy", action]
        if kind == "duel_accept" and accepted:
            action = "defer"
            path.append("one_accept_per_team_per_tick")
        elif kind == "duel_accept":
            accepted = True
        elif duel_id in offered:
            action = "already_sent"
            path.append("local_action_memory")
        facts = decision_facts(by_id.get(duel_id, {}), tick,
                               same_deadline.get(by_id.get(duel_id, {}).get("deadline_tick"), 1))
        out.append({"duel": duel_id, "action": action, "candidate": candidate,
                    "path": path, "trigger": _trigger(action, facts), "facts": facts,
                    "reason": candidate.get("why", ""),
                    "feed": evidence})
    for duel in live:
        duel_id = duel.get("duel")
        if duel_id in chosen:
            continue
        reason, path = _waiting_reason(duel, tick)
        facts = decision_facts(duel, tick,
                               same_deadline.get(duel.get("deadline_tick"), 1))
        out.append({"duel": duel_id, "action": "wait", "candidate": None,
                    "path": path, "trigger": reason, "facts": facts,
                    "reason": reason, "feed": evidence})
    return out
