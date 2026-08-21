"""Fixtures for ff_net integration tests.

These hit a REAL Cassandra cluster (whatever CASSANDRA_HOSTS/PORT the
running settings point at, default 127.0.0.1:9042) -- there's no SQL DB in
this app to give Django's usual test-database isolation, so tests use their
own reserved net_id range (999100+) and clean up after themselves via
fixture teardown instead.
"""
import datetime as dt

import pytest

from apps.core import cassandra_session
from apps.ff_net.submodules.ff_net_repository import (
    ff_net_building_repository,
    ff_net_merge_candidates_repository,
)

# Reserved for tests so they never collide with real demo/prod data.
TEST_NET_BASE = 999100


@pytest.fixture(scope="session", autouse=True)
def cassandra_ready():
    if not cassandra_session.is_ready():
        cassandra_session.connect()
    if not cassandra_session.is_ready():
        pytest.skip(
            "Cassandra is not reachable at CASSANDRA_HOSTS -- these are "
            "integration tests against a real cluster, not mocked. Start "
            "the cassandra-database container and re-run."
        )


@pytest.fixture
def make_net():
    """Factory: insert a ff_net_building row with sane, mutually-matching
    defaults (same frequency/time/location) so two make_net() calls are a
    merge candidate unless a test deliberately overrides a field to push
    them apart. Cleans up every net_id it created after the test."""
    created = []

    def _make(net_id, my_net_id=None, frequency=1000.0, bandwidth=10.0,
              lti=None, latitude=10.0, longitude=20.0, rdfs="RDFS1", **extra):
        row = {
            "net_id": net_id,
            "my_net_id": net_id if my_net_id is None else my_net_id,
            "ff_id": net_id,
            "frequency": frequency,
            "bandwidth": bandwidth,
            "lti": lti or dt.date(2026, 1, 1),
            "fti": lti or dt.date(2026, 1, 1),
            "latitude": latitude,
            "longitude": longitude,
            "rdfs": rdfs,
            "nti": 1,
            "updated_at": dt.datetime.utcnow(),
        }
        row.update(extra)
        ff_net_building_repository.insert(row)
        created.append(net_id)
        return row

    yield _make

    for net_id in created:
        ff_net_building_repository.delete({"net_id": net_id})


@pytest.fixture
def cleanup_candidates():
    """Register (net_a, net_b) pairs to delete from ff_net_merge_candidates
    after the test, regardless of what status they end up in."""
    pairs = []

    def _register(net_a, net_b):
        pairs.append(tuple(sorted((net_a, net_b))))

    yield _register

    for net_a, net_b in pairs:
        ff_net_merge_candidates_repository.delete({"net_a": net_a, "net_b": net_b})
