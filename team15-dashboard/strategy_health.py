"""Evidence for the Sunday decision desk. Reads local reports and settled duels only."""
from __future__ import annotations

import json
from pathlib import Path


def _latest(roots, name: str):
    paths = [Path(root) / 'data' / name for root in roots]
    existing = [p for p in paths if p.is_file()]
    return max(existing, key=lambda p: p.stat().st_mtime) if existing else None


def _json(path):
    if path is None:
        return None
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def build(done: list[dict], roots, tick: int | None) -> dict:
    """Separate server outcomes from a local learner's actual integration state."""
    sessions = []
    for session in sorted({d.get('session') for d in done if isinstance(d.get('session'), int)}):
        if session == 1:  # practice does not score
            continue
        cohort = [d for d in done if d.get('session') == session
                  and d.get('status') in ('deal', 'no_deal')]
        positive = sum(d.get('status') == 'deal' and isinstance(d.get('result'), (int, float))
                       and d['result'] > 0 for d in cohort)
        negative = sum(d.get('status') == 'deal' and isinstance(d.get('result'), (int, float))
                       and d['result'] < 0 for d in cohort)
        zero_deal = sum(d.get('status') == 'deal' for d in cohort) - positive - negative
        no_deal = sum(d.get('status') == 'no_deal' for d in cohort)
        sessions.append({'session': session, 'total': len(cohort), 'positive': positive,
                         'negative': negative, 'zero_deal': zero_deal, 'no_deal': no_deal})

    report_path = _latest(roots, 'agent_memory_report.json')
    report = _json(report_path)
    db_path = _latest(roots, 'agent_memory.sqlite3')
    if report:
        lag = tick - report.get('tick', tick) if isinstance(tick, int) and isinstance(report.get('tick'), int) else None
        memory = {'status': 'fresh' if lag is not None and 0 <= lag <= 40 else 'stale',
                  'last_tick': report.get('tick'), 'ticks_behind': lag,
                  'stored_events': report.get('stored_events'),
                  'events_in_window': report.get('events_in_window'),
                  'own_settlements': len(report.get('own_single_card_settlements') or [])}
    else:
        memory = {'status': 'database_only' if db_path else 'not_started',
                  'last_tick': None, 'stored_events': None, 'events_in_window': None,
                  'own_settlements': None}

    learner_path = _latest(roots, 'duel_learning.json')
    learner = _json(learner_path)
    learner_facts = learner.get('facts') or {} if learner else {}
    duel_learning = {'status': 'updated' if learner else 'not_started',
                     'duels_done': learner_facts.get('duels_done'),
                     'deals': learner_facts.get('deals'),
                     'note': 'Un modelo cargado no demuestra mejora; validar con replay temporal.'}
    return {'duels': {'source': '/api/duels?done=true' if done else 'unavailable',
                      'sessions': sessions,
                      'settled': sum(s['total'] for s in sessions),
                      'negative': sum(s['negative'] for s in sessions),
                      'no_deal': sum(s['no_deal'] for s in sessions)},
            'memory': memory, 'duel_learning': duel_learning}
