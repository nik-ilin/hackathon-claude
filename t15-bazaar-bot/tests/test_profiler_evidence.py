"""A price-based rarity guess must not become confirmed training evidence."""
from intel.profiler import haggles, dealer_profiles


def test_unknown_rarity_keeps_price_guess_separate():
    def query(sql):
        if sql == 'SELECT id, topic FROM threads':
            return [(1, '{"buy":{"rarity":"common"}}'),
                    (2, '{"buy":{"card":"LAV-01"}}')]
        if 'FROM provenance' in sql:
            return []
        if 'FROM thread_messages' in sql:
            return [(1, 10, 't15', 'abuela', 'abuela', 10, False),
                    (2, 11, 't15', 'abuela', 'abuela', 11, False)]
        if 'FROM settlements' in sql:
            return []
        raise AssertionError(sql)

    rows = haggles(query, {'abuela'})
    unknown = next(row for row in rows if row['thread'] == 2)
    assert unknown['kind'] == 'carta'
    assert unknown['kind_estimate'] == 'common'
    profiles = dealer_profiles(rows)
    assert profiles['abuela|buy|common']['threads'] == 1
    assert profiles['abuela|buy|carta']['threads'] == 1
