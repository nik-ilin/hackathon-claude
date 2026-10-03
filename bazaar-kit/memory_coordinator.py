"""Coordinator with persistent feed evidence, using the existing trading authority.

    python3 memory_coordinator.py                       # analysis, saves evidence
    python3 memory_coordinator.py --execute --ticks 30   # existing coordinator limits

An adapter keeps the independently maintained coordinator unmodified. It only enriches
its analysis snapshot: network operations, reconciliation, inventory guards, budgets
and its shared process lock remain in coordinator.py.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager

import agent_memory
import coordinator


@contextmanager
def integrated(path=agent_memory.DEFAULT):
    original_candidates, original_cycle = coordinator.candidates, coordinator.cycle
    latest = {}

    def candidates(snapshot, ledger, args, journal):
        latest['snapshot'] = snapshot
        try:
            enriched, report = agent_memory.prepare(
                snapshot, ledger.get('actions', []), path=path,
                window=getattr(args, 'history_window', 240),
                market_actions=coordinator.ma.load_json(coordinator.ma.LEDGER).get('actions', []))
            coordinator.ma.save(coordinator.DATA / 'agent_memory_report.json', report)
            coordinator.MEMORY_STATUS.update(integrated=True, ok=True, error=None, stored_events=report['stored_events'],
                                             events_in_window=report['events_in_window'])
            print(f"   MEMORIA: {report['stored_events']} eventos persistidos; "
                  f"{report['events_in_window']} en ventana; "
                  f"{len(report['own_single_card_settlements'])} liquidaciones propias de una carta observadas")
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as e:
            # Memory failure must not change the operational guards or stop reconciliation.
            coordinator.MEMORY_STATUS.update(integrated=True, ok=False, error=type(e).__name__)
            print(f'   MEMORIA no disponible ({type(e).__name__}); análisis con snapshot actual')
            return original_candidates(snapshot, ledger, args, journal)
        result = original_candidates(enriched, ledger, args, journal)
        agent_memory.annotate(result[0], report)
        return result

    def cycle(reader, args, ledger, journal, execute, cache=None):
        latest.clear()
        try:
            return original_cycle(reader, args, ledger, journal, execute, cache)
        finally:
            if 'snapshot' in latest:
                try:
                    memory = agent_memory.Memory(path)
                    try:
                        memory.capture(latest['snapshot'], ledger.get('actions', []))
                    finally:
                        memory.close()
                except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as e:
                    print(f'   MEMORIA: no se pudo guardar el resultado ({type(e).__name__})')

    coordinator.candidates, coordinator.cycle = candidates, cycle
    try:
        yield
    finally:
        coordinator.candidates, coordinator.cycle = original_candidates, original_cycle


def main():
    with integrated():
        coordinator.main()


if __name__ == '__main__':
    main()
