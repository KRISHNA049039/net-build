"""Single shared Cassandra Cluster/Session + readiness flag."""
import logging
import threading
import time

from cassandra import ConsistencyLevel
from cassandra.cluster import Cluster
from cassandra.policies import DCAwareRoundRobinPolicy, TokenAwarePolicy
from django.conf import settings

log = logging.getLogger(__name__)

_lock = threading.Lock()
_cluster = None
_session = None
_ready = False
_poller_started = False


def is_ready():
    """Live readiness: session exists AND the driver sees at least one up host."""
    if _session is None:
        return False
    try:
        hosts = _session.cluster.metadata.all_hosts()
        return any(h.is_up for h in hosts)
    except Exception:
        return False

def get_session():
    """Return the shared Session, or None if not connected yet."""
    return _session


def connect():
    """Attempt one connect. Sets readiness on success. Returns True/False."""
    global _cluster, _session, _ready
    cfg = settings.CASSANDRA
    try:
        with _lock:
            if _session is not None:
                return True
            cluster = Cluster(
                contact_points=cfg["HOSTS"],
                port=cfg["PORT"],
                load_balancing_policy=TokenAwarePolicy(
                    DCAwareRoundRobinPolicy(local_dc=cfg["LOCAL_DC"])
                ),
                protocol_version=5,
            )
            session = cluster.connect(cfg["KEYSPACE"]) if cfg["KEYSPACE"] else cluster.connect()
            session.default_consistency_level = getattr(
                ConsistencyLevel, cfg["CONSISTENCY_LEVEL"]
            )
            _cluster = cluster
            _session = session
            _ready = True
        log.info("Cassandra connected (keyspace=%s)", cfg["KEYSPACE"])
        return True
    except Exception as exc:
        _ready = False
        log.warning("Cassandra connect failed: %s", exc)
        return False


def shutdown():
    global _cluster, _session, _ready
    with _lock:
        if _cluster is not None:
            _cluster.shutdown()
        _cluster = None
        _session = None
        _ready = False


def _poll_loop(interval=10):
    while True:
        if not _ready:
            connect()
        time.sleep(interval)


def start_poller():
    """Spawn exactly one daemon poller. Idempotent."""
    global _poller_started
    with _lock:
        if _poller_started:
            return
        _poller_started = True
    t = threading.Thread(target=_poll_loop, name="cassandra-poller", daemon=True)
    t.start()
    log.info("Cassandra poller started")