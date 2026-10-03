"""Small, conservative interpreter for public Radio Rastro news."""
from __future__ import annotations

import re
import unicodedata


def normalized(value: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFKD', value.lower())
                   if not unicodedata.combining(c))


def radio_event(event: dict) -> dict | None:
    payload = event.get('payload') or {}
    if event.get('type') != 'news.posted' or payload.get('source') != 'radio':
        return None
    return {'id': event.get('id'), 'tick': event.get('tick'),
            'source': payload.get('source_name') or 'Radio',
            'headline': payload.get('headline') or '',
            'body': payload.get('body') or ''}


def interpret(news: list[dict], catalog: dict, guide: list[dict], current_tick: int = 0) -> list[dict]:
    cards = {c['id']: c for s in catalog.get('sets', []) for c in s.get('cards', [])}
    sets = {s['id']: s for s in catalog.get('sets', [])}
    out = []
    for item in sorted(news, key=lambda n: (n.get('tick') or 0, n.get('id') or 0), reverse=True)[:4]:
        words = normalized(item['headline'] + ' ' + item['body'])
        named_sets = {sid for sid, s in sets.items()
                      if (s.get('name') and normalized(s['name']) in words) or
                      re.search(r'\b' + re.escape(sid.lower()) + r'\b', words)}
        rarity = None
        for pattern, value in ((r'\b(legendary|legendaria|legendarias)\b', 'legendary'),
                               (r'\b(epic|epica|epicas)\b', 'epic'),
                               (r'\b(rare|raras?|raros?)\b', 'rare'),
                               (r'\b(uncommon|infrecuentes?)\b', 'uncommon'),
                               (r'\b(common|comunes?)\b', 'common')):
            if re.search(pattern, words):
                rarity = value
                break
        explicit_refs = set(re.findall(r'\b[A-Z]{3}-\d{2}\b', (item['headline'] + ' ' + item['body']).upper()))
        matches = [g for g in guide if g['ref'] in explicit_refs or
                   (g['ref'].split('-')[0] in named_sets and
                    (rarity is None or cards.get(g['ref'], {}).get('rarity') == rarity))]
        one_hour = bool(re.search(r'\b(one hour|una hora)\b', words))
        expired = one_hour and current_tick and item.get('tick') and current_tick > item['tick'] + 120
        if expired:
            action = 'La hora anunciada ya pasó. Consulta una cotización actual antes de tomar decisiones con esta noticia.'
        elif matches:
            refs = ', '.join(g['ref'] for g in matches)
            action = (f'La noticia podría afectar {refs}. Comprueba una puja o cotización real antes de cambiar precios; '
                      'el máximo que puedes ceder por carta figura en la guía de arriba.')
        elif named_sets or explicit_refs:
            action = ('No coincide con nuestros duplicados libres. Mantén los precios de la guía; '
                      'no concedas ni subas por este rumor.')
        else:
            action = 'Sin efecto verificable en nuestros duplicados libres; no cambiar precios por esta noticia.'
        out.append({**item, 'matches': [g['ref'] for g in matches], 'action': action})
    return out
