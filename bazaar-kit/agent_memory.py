"""Persistent evidence for the coordinator. No network or game writes.

Raw feed and decision snapshots are retained in SQLite. Only past events in a bounded
window enter pricing. Repeated polls do not create observations. Historical dealer
quotes and settled prices are reported separately, never treated as guaranteed floors.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from feed_oracle import Oracle, read_settlements

DEFAULT = Path(__file__).resolve().parent / 'data' / 'agent_memory.sqlite3'
SENSITIVE = {'broker_key', 'starter_broker_key', 'team_key', 'api_key', 'authorization', 'x-team-key', 'x-broker-key'}
ACTION_FIELDS = {'key', 'type', 'module', 'kind', 'tick', 'status', 'offer', 'asset', 'assets', 'ref',
                 'price', 'paid', 'venue', 'dealer', 'item', 'thread', 'du', 'dv', 'cash', 'fee',
                 'expected_du', 'p_fill', 'settled_tick', 'reason', 'version'}


def clean(value):
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items() if k.lower() not in SENSITIVE}
    if isinstance(value, list):
        return [clean(v) for v in value]
    return value


class Memory:
    def __init__(self, path=DEFAULT):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=5)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, tick INTEGER NOT NULL, body TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS event_tick ON events(tick);
        CREATE TABLE IF NOT EXISTS imports (path TEXT PRIMARY KEY, inode INTEGER, offset INTEGER);
        CREATE TABLE IF NOT EXISTS actions (
            team TEXT, identity TEXT, body TEXT, PRIMARY KEY(team,identity));
        CREATE TABLE IF NOT EXISTS action_history (
            team TEXT, identity TEXT, status TEXT, observed_tick INTEGER, body TEXT,
            PRIMARY KEY(team,identity,status));
        CREATE TABLE IF NOT EXISTS observations (
            team TEXT, tick INTEGER, source TEXT, body TEXT, PRIMARY KEY(team,tick,source));
        ''')

    def close(self):
        self.db.close()

    def import_history(self, path, tick):
        """Incremental import of the oracle collector JSONL; retain incomplete final lines for next poll."""
        path = Path(path)
        if not path.exists():
            return 0
        info = path.stat()
        row = self.db.execute('SELECT inode,offset FROM imports WHERE path=?', (str(path),)).fetchone()
        offset = row[1] if row and row[0] == info.st_ino and row[1] <= info.st_size else 0
        before = self.db.total_changes
        with path.open('rb') as f, self.db:
            f.seek(offset)
            while True:
                start = f.tell()
                line = f.readline()
                if not line or not line.endswith(b'\n'):
                    offset = start
                    break
                try:
                    e = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    offset = f.tell()
                    continue
                if not isinstance(e, dict) or not isinstance(e.get('id'), int) or not isinstance(e.get('tick'), int):
                    offset = f.tell()
                    continue
                if e['tick'] > tick:
                    offset = start
                    break
                self.db.execute('INSERT OR IGNORE INTO events VALUES (?,?,?)',
                                (e['id'], e['tick'], json.dumps(clean(e), ensure_ascii=False)))
                offset = f.tell()
            fresh = self.db.total_changes - before
            self.db.execute('INSERT OR REPLACE INTO imports VALUES (?,?,?)', (str(path), info.st_ino, offset))
        return fresh

    def capture(self, snapshot, actions=(), source='coordinator'):
        tick, me = snapshot['clock']['tick'], snapshot['me']
        team = me['id']
        before = self.db.total_changes
        with self.db:
            for e in snapshot.get('feed', {}).get('events', []):
                if not isinstance(e, dict) or not isinstance(e.get('id'), int) or not isinstance(e.get('tick'), int):
                    continue
                if e['tick'] > tick:
                    continue
                self.db.execute('INSERT OR IGNORE INTO events VALUES (?,?,?)',
                                (e['id'], e['tick'], json.dumps(clean(e), ensure_ascii=False)))
            fresh = self.db.total_changes - before
            import learning  # momento en que NOSOTROS vimos cada evento: replay sin fuga temporal
            learning.mark_seen(self.db, [e for e in snapshot.get('feed', {}).get('events', [])
                                         if isinstance(e, dict) and isinstance(e.get('tick'), int) and e['tick'] <= tick],
                               tick, source)
            for a in actions:
                if not a.get('key'):
                    continue
                # Include tick/type: don't conflate reissued intents or different ledgers.
                ident = f"{source}:{a['key']}:{a.get('tick')}:{a.get('type')}"
                body = {k: clean(v) for k, v in a.items() if k in ACTION_FIELDS}
                encoded = json.dumps(body, ensure_ascii=False)
                self.db.execute('INSERT OR IGNORE INTO action_history VALUES (?,?,?,?,?)',
                                (team, ident, a.get('status', 'unknown'), tick, encoded))
                previous = self.db.execute('SELECT body FROM actions WHERE team=? AND identity=?',
                                           (team, ident)).fetchone()
                if previous and json.loads(previous[0]).get('status') == 'settled' and a.get('status') != 'settled':
                    continue  # a stale ledger must not erase a confirmed result
                self.db.execute('INSERT OR REPLACE INTO actions VALUES (?,?,?)', (team, ident, encoded))
            obs = {k: clean(me.get(k)) for k in ('cash', 'collection_value', 'album', 'score')}
            obs['round'] = snapshot['clock'].get('round')
            self.db.execute('INSERT OR REPLACE INTO observations VALUES (?,?,?,?)',
                            (team, tick, source, json.dumps(obs, ensure_ascii=False)))
        return fresh

    def events(self, tick, window=240):
        return [json.loads(row[0]) for row in self.db.execute(
            'SELECT body FROM events WHERE tick BETWEEN ? AND ? ORDER BY tick,id',
            (max(0, tick-window), tick))]

    def enrich(self, snapshot, window=240):
        """Historical feed is analysis-only. Current boards, holdings and fees stay authoritative."""
        past = self.events(snapshot['clock']['tick'], window)
        merged = {e['id']: e for e in past}
        for e in snapshot.get('feed', {}).get('events', []):
            if (isinstance(e.get('id'), int) and
                    max(0, snapshot['clock']['tick']-window) <= e.get('tick', 0) <= snapshot['clock']['tick']):
                merged[e['id']] = e
        return {**snapshot, 'feed': {**snapshot.get('feed', {}), 'events': sorted(
            merged.values(), key=lambda e: (e.get('tick', 0), e['id']))}}

    def report(self, snapshot, window=240):
        tick, team = snapshot['clock']['tick'], snapshot['me']['id']
        events = self.events(tick, window)
        oracle = Oracle()
        oracle.load_catalog(snapshot.get('catalog', {}))
        oracle.ingest(events)
        dealers = []
        for (dealer, ref, side), line in sorted(oracle.dealers.items()):
            dealers.append({'dealer': dealer, 'ref': ref, 'side': side,
                            'settled_count': len(line.settled),
                            'best_settled_observed': (min(line.settled) if side == 'ask' else max(line.settled))
                            if line.settled else None,
                            'quoted_count': len(line.prices), 'final_quotes': line.finals,
                            'basis': 'historical evidence, not a guaranteed limit'})
        own = [vars(s) for s in read_settlements(events) if team in (s.seller, s.buyer)]
        actions = [json.loads(r[0]) for r in self.db.execute('SELECT body FROM actions WHERE team=?', (team,))]
        states = {}
        for a in actions:
            state = a.get('status', 'unknown')
            states[state] = states.get(state, 0) + 1
        count = self.db.execute('SELECT count(*) FROM events').fetchone()[0]
        return {'tick': tick, 'team': team, 'stored_events': count, 'window_ticks': window,
                'events_in_window': len(events), 'dealer_evidence': dealers,
                'own_single_card_settlements': own, 'recorded_action_states': states,
                'limits': ['feed coverage may be incomplete', 'open/cancelled is not a price rejection',
                           'observed cash prices are not net profit or score',
                           'bundles excluded from single-card price statistics',
                           'historical evidence never overrides live holdings, value guards or cash limits']}


def prepare(snapshot, actions, path=DEFAULT, window=240, market_actions=()):
    memory = Memory(path)
    try:
        memory.import_history(Path(path).parent / 'feed_history.jsonl', snapshot['clock']['tick'])
        memory.capture(snapshot, actions)
        memory.capture(snapshot, market_actions, source='market')
        return memory.enrich(snapshot, window), memory.report(snapshot, window)
    finally:
        memory.close()


def annotate(candidates, report):
    """Explain past evidence; do not relax reservations or rewrite prices from a historical minimum."""
    lines = {(r['dealer'], r['ref'], r['side']): r for r in report['dealer_evidence']}
    for c in candidates:
        ref = (c.get('ref') or c.get('item') or '').removeprefix('card:')
        row = lines.get((c.get('dealer'), ref, 'ask'))
        if row and row['settled_count']:
            c.setdefault('notes', []).append(f"memoria: mejor compra observada {row['best_settled_observed']} P "
                                             f"en {row['settled_count']} liquidaciones; no es un suelo garantizado")
            c['memory_evidence'] = row


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db', type=Path, default=DEFAULT)
    args = p.parse_args()
    if not args.db.exists():
        p.error('Todavía no hay memoria: ejecutar el coordinador primero')
    m = Memory(args.db)
    try:
        print('Eventos guardados:', m.db.execute('SELECT count(*) FROM events').fetchone()[0])
        for row in m.db.execute('SELECT team, max(tick), count(*) FROM observations GROUP BY team'):
            print('Equipo, último tick, observaciones:', row)
    finally:
        m.close()


if __name__ == '__main__':
    main()
