"""Bind + execute a prepared statement, return rows as JSON-safe dicts."""
import datetime
import uuid
from decimal import Decimal

from cassandra.util import Date, Time

from . import cassandra_session


def _coerce(v):
    if isinstance(v, Date):
        return str(v)          # -> 'YYYY-MM-DD'
    if isinstance(v, Time):
        return str(v)          # -> 'HH:MM:SS.ffffff'
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, (datetime.datetime, datetime.date, datetime.time)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, bytes):
        return v.hex()
    return v


def rows_to_dicts(result_set):
    return [{k: _coerce(v) for k, v in row._asdict().items()} for row in result_set]


def execute(prepared, params=None):
    session = cassandra_session.get_session()
    if session is None:
        raise RuntimeError("Cassandra session not ready")
    result = session.execute(prepared, params or [])
    return rows_to_dicts(result)