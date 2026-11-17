"""Regression checks for independent completion work before team integration."""
from datetime import datetime, timezone
import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy import select
from discovery.app.dates import parse_query
from discovery.app.embeddings import Ollama
from discovery.app.store import Entry


@pytest.mark.parametrize('query', [' ', 'music Friday morning', 'music next weekend',
                                  'music at 7pm', 'music before midnight', 'music yesterday'])
def test_time_constraints_are_never_silently_discarded(query):
    with pytest.raises(ValueError):
        parse_query(query, datetime(2026, 11, 2, tzinfo=timezone.utc))


def test_pgvector_query_uses_cosine_distance_operator():
    statement = select(1 - Entry.embedding.cosine_distance([1.0, 0.0]))
    assert '<=>' in str(statement.compile(dialect=postgresql.dialect()))


@pytest.mark.parametrize('config', [{'timeout': 0}, {'price': -1}, {'capacity': 5}, {'threshold': 0}, {'cooldown': -1}])
def test_invalid_resilience_configuration_fails_fast(config):
    with pytest.raises(ValueError):
        Ollama(**config)
