from datetime import datetime, timezone
import pytest
from discovery.app.dates import parse_query

NOW = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)


def test_friday_night_filters_before_ranking():
    result = parse_query('Something calm with music on Friday night', NOW)
    assert result.semantic_text == 'calm music'
    assert result.matches(datetime(2026, 11, 6, 20, tzinfo=timezone.utc))
    assert not result.matches(datetime(2026, 11, 6, 10, tzinfo=timezone.utc))
    assert not result.matches(datetime(2026, 11, 7, 20, tzinfo=timezone.utc))


def test_tonight_never_includes_the_past():
    now = datetime(2026, 11, 2, 22, tzinfo=timezone.utc)
    result = parse_query('music tonight', now)
    assert not result.matches(datetime(2026, 11, 2, 21, tzinfo=timezone.utc))
    assert result.matches(datetime(2026, 11, 2, 23, tzinfo=timezone.utc))


def test_weekend_and_after():
    result = parse_query('music this weekend after 7pm', NOW)
    assert not result.matches(datetime(2026, 11, 7, 19, tzinfo=timezone.utc))
    assert result.matches(datetime(2026, 11, 7, 20, tzinfo=timezone.utc))
    assert result.matches(datetime(2026, 11, 8, 20, tzinfo=timezone.utc))
    assert not result.matches(datetime(2026, 11, 9, 20, tzinfo=timezone.utc))


def test_sunday_means_current_weekend():
    result = parse_query('this weekend', datetime(2026, 11, 8, 12, tzinfo=timezone.utc))
    assert result.matches(datetime(2026, 11, 8, 20, tzinfo=timezone.utc))


def test_dst_uses_local_midnights():
    result = parse_query('this weekend', datetime(2026, 10, 23, 12, tzinfo=timezone.utc))
    assert (result.end - result.start).total_seconds() == 49 * 3600


@pytest.mark.parametrize('query', ['Friday Saturday music', 'next Friday', 'music after 25pm', 'music before 7pm', 'music 2026-11-06'])
def test_unsupported_or_ambiguous_time_is_rejected(query):
    with pytest.raises(ValueError):
        parse_query(query, NOW)
