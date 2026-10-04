"""Versioned read-only API adapters for dashboard and decision agents."""
from __future__ import annotations

from typing import Callable

import market_activity


SECTION_SOURCES = {
    "overview": ["/api/me", "/api/leaderboard", "/api/clock", "local:score_history"],
    "ranking": ["/api/leaderboard", "/api/me", "local:score_history"],
    "operations": ["/api/duels", "/api/me/offers", "/api/venues"],
    "duels": ["/api/duels", "/api/duels?done=true", "local:duels_log.jsonl"],
    "market": ["/api/venues", "/api/venues/{venue}/offers", "/api/me/offers"],
    "trading": ["/api/me/offers", "local:feed_history.jsonl", "local:score_history.jsonl"],
    "collection": ["/api/me", "/api/catalog", "/api/venues", "local:feed_history.jsonl"],
    "rivals": ["/api/leaderboard", "local:feed_history.jsonl"],
    "signals": ["/api/news", "/api/feed", "local:radio"],
    "agents": ["local:agent.lock", "local:agent_memory_report.json", "local:duel_learning.json"],
}


def export(data: dict, v2_export: Callable[[dict], dict]) -> dict:
    """Keep source evidence and expose stable sections without changing the v2 contract."""
    v2 = v2_export(data)
    ops = v2.get("operations") or {}
    scoring = v2.get("scoring") or {}
    rankings = v2.get("ranking") or {}
    strategy_health = v2.get("strategy_health") or {}
    activity = v2.get("market_activity") or {}
    opportunities = v2.get("opportunities") or []
    sources = {
        "private_api": {"status": "fresh" if data.get("verified") else "unavailable",
                         "tick": data.get("tick"), "last_success_at": data.get("built_at")},
        "public_feed": {"status": (data.get("feed_health") or {}).get("status", "unknown"),
                        "tick": data.get("tick"), "last_tick": (data.get("feed_health") or {}).get("last_tick"),
                        "ticks_behind": (data.get("feed_health") or {}).get("ticks_behind")},
        "leaderboard": {"status": ("fresh" if (rankings.get("source") == "live") else
                                    "stale" if (rankings.get("source") == "history") else "unavailable"),
                        "tick": rankings.get("observed_tick"),
                        "current_tick": data.get("tick"), "source": rankings.get("source")},
        "market_books": {"status": "partial" if data.get("warnings") else "available",
                         "venues": data.get("venue_count"), "offers": data.get("board_count"),
                         "tick": data.get("tick")},
    }
    snapshot = {"id": f"t15-{data.get('tick', 'unknown')}", "team": "t15", "tick": data.get("tick"),
                "captured_at": data.get("built_at"), "verified_private_data": bool(data.get("verified")),
                "read_only": True, "clock": data.get("clock") or {},
                "tick_time_context": data.get("tick_time_context") or {}}
    sections = {
        "overview": {"ranking": rankings, "scoring": scoring, "top_actions": (data.get("trades") or [])[:6],
                     "opportunities": opportunities[:6], "capital": ops.get("capital"),
                     "queue": ops.get("queue"), "market": activity.get("summary"),
                     "my_offers": activity.get("my_open_offers") or [],
                     "incoming_offers": activity.get("incoming_offers") or [],
                     "public_offers": activity.get("own_market_offers") or [],
                     "group_share_message": market_activity.group_share_message(activity),
                     "cards": v2.get("cards") or [],
                     "my_sales": activity.get("my_active_sales") or [],
                     "own_venue": {"id": activity.get("own_venue"), "name": activity.get("own_venue_name")},
                     "health": sources, "leaderboard_history": v2.get("leaderboard_history") or {},
                     "impacts": v2.get("score_impacts") or []},
        "ranking": {"ranking": rankings, "leaderboard_history": v2.get("leaderboard_history") or {},
                    "scoring": scoring, "peers": v2.get("peers") or {},
                    "score_impacts": v2.get("score_impacts") or [],
                    "score_history": data.get("history") or [], "history_summary": data.get("history_summary") or {}},
        "operations": {"operations": ops, "strategy_health": strategy_health,
                       "duels": ops.get("duels") or {}, "commands": _commands(data),
                       "my_offers": activity.get("my_open_offers") or [],
                       "incoming_offers": activity.get("incoming_offers") or [],
                       "market_summary": activity.get("summary") or {}},
        "duels": {"live": ops.get("duels") or {}, "history": data.get("duel_history") or [],
                  "rows": v2.get("duel_history") or [], "analysis": v2.get("duel_analysis") or {},
                  "strategy_health": strategy_health},
        "market": {"activity": activity, "score": scoring, "venue": ops.get("market") or {},
                   "offers": activity.get("all_offers") or [],
                   "own_market_offers": activity.get("own_market_offers") or [],
                   "incoming_offers": activity.get("incoming_offers") or [],
                   "summary": activity.get("summary") or {}, "kpis": v2.get("kpis") or {}},
        "trading": {"history": v2.get("trade_history") or [], "summary": v2.get("trade_summary") or {},
                    "opportunities": data.get("trades") or [], "proposals": opportunities,
                    "sale_guide": data.get("sale_guide") or [], "liquidation_targets": v2.get("liquidation_targets") or [],
                    "ladder": v2.get("ladder") or {}},
        "collection": {"catalog": v2.get("cards") or [], "inventory": data.get("inventory") or [],
                       "needs": data.get("needs") or [], "packs": v2.get("unopened_packs") or [],
                       "kpis": v2.get("kpis") or {}, "ratios": v2.get("ratios") or {}},
        "rivals": {"teams": data.get("teams") or [], "peers": v2.get("peers") or {},
                   "leaderboard": rankings.get("leaderboard_order") or []},
        "signals": {"radio": data.get("radio") or [], "summary": data.get("radio_summary") or {},
                    "impacts": v2.get("score_impacts") or []},
        "agents": {"health": strategy_health, "operations": ops, "sources": sources,
                   "history": data.get("history_summary") or {}},
    }
    for section_data in sections.values():
        section_data["snapshot"] = snapshot
    return {"schema": "team15.dashboard.v3", "snapshot": snapshot, "sources": sources,
            "sections": sections, "definitions": v2.get("definitions") or {}}


def _commands(data: dict) -> list[dict]:
    """Copyable examples only; the API and web UI never execute these commands."""
    return [{"id": "duels-day3", "label": "Ejecutar Duelos III", "cwd": "bazaar-kit",
             "command": "./run.sh duels --day3 --execute", "writes_to_game": True,
             "note": "Pega en una terminal de operación, no en la terminal del dashboard."},
            {"id": "duels-preview", "label": "Previsualizar decisiones de Duelos III", "cwd": "bazaar-kit",
             "command": "./run.sh duels --day3", "writes_to_game": False,
             "note": "Solo análisis; no acepta ni publica ofertas."}]
