"""Shared Cassandra repository base.

Provides prepared-statement caching plus generic CRUD, filtered reads, and
cursor-based pagination.

Development note: every *filtered* read appends ALLOW FILTERING so that
queries on non-key columns don't fail while the schema is still evolving.
Once the real access patterns are known, tables can be redesigned around
proper partition/clustering keys and ALLOW FILTERING dropped.
"""
import base64

from apps.core import cassandra_session
from apps.core.execution import rows_to_dicts


class BaseRepository:
    keyspace = None
    table = None
    # Ordered tuple of the columns that make up the primary key.
    primary_key_columns = ()

    def __init__(self):
        self._prepared_statements = {}

    # -- session / statement helpers ------------------------------------
    def _session(self):
        session = cassandra_session.get_session()
        if session is None:
            raise RuntimeError("Cassandra session not ready")
        return session

    def _prepared_statement(self, cql):
        statement = self._prepared_statements.get(cql)
        if statement is None:
            statement = self._session().prepare(cql)
            self._prepared_statements[cql] = statement
        return statement

    def _execute(self, cql, params=None):
        """Run a statement and return the raw driver result (for writes)."""
        return self._session().execute(self._prepared_statement(cql), params or [])

    def _fetch(self, cql, params=None):
        """Run a statement and return a list of dict rows (for reads)."""
        return rows_to_dicts(self._execute(cql, params))

    def _select_cql(self, filter_columns):
        """Build a SELECT with an equality WHERE clause (no LIMIT here).

        ALLOW FILTERING is appended whenever any filter is present so that
        non-key columns can be queried during development.
        """
        cql = f"SELECT * FROM {self.keyspace}.{self.table}"
        if filter_columns:
            where_clause = " AND ".join(f"{column} = ?" for column in filter_columns)
            cql += f" WHERE {where_clause} ALLOW FILTERING"
        return cql

    # -- reads -----------------------------------------------------------
    def find(self, filters=None, limit=100):
        """Return rows matching equality on any column(s), capped by LIMIT.

        Unchanged single-shot read used by `get`. For paged listing use
        `find_page` instead.
        """
        filters = filters or {}
        filter_columns = list(filters.keys())

        cql = self._select_cql(filter_columns)
        params = [filters[column] for column in filter_columns]

        if limit is not None:
            # ALLOW FILTERING must stay last in CQL, so insert LIMIT before it.
            if " ALLOW FILTERING" in cql:
                cql = cql.replace(" ALLOW FILTERING", " LIMIT ? ALLOW FILTERING")
            else:
                cql += " LIMIT ?"
            params.append(limit)

        return self._fetch(cql, params)

    def find_page(self, filters=None, page_size=100, paging_state=None):
        """Return one page of rows plus the cursor for the next page.

        `paging_state` is the opaque driver cursor (bytes) or None for the
        first page. Returns (rows, next_paging_state); next_paging_state is
        None when there are no further pages.
        """
        filters = filters or {}
        filter_columns = list(filters.keys())

        cql = self._select_cql(filter_columns)
        params = [filters[column] for column in filter_columns]

        bound = self._prepared_statement(cql).bind(params)
        bound.fetch_size = page_size
        result = self._session().execute(bound, paging_state=paging_state)

        rows = rows_to_dicts(result.current_rows)
        return rows, result.paging_state

    def get(self, key_values):
        """Fetch a single row by its full primary key (dict column -> value)."""
        rows = self.find(filters=key_values, limit=1)
        return rows[0] if rows else None

    # -- writes ----------------------------------------------------------
    def insert(self, row):
        columns = list(row.keys())
        column_list = ", ".join(columns)
        placeholders = ", ".join(["?"] * len(columns))
        cql = (
            f"INSERT INTO {self.keyspace}.{self.table} "
            f"({column_list}) VALUES ({placeholders})"
        )
        self._execute(cql, [row[column] for column in columns])

    def update(self, key_values, fields):
        assignments = ", ".join(f"{column} = ?" for column in fields)
        conditions = " AND ".join(f"{column} = ?" for column in key_values)
        params = list(fields.values()) + list(key_values.values())
        cql = (
            f"UPDATE {self.keyspace}.{self.table} "
            f"SET {assignments} WHERE {conditions}"
        )
        self._execute(cql, params)

    def delete(self, key_values):
        conditions = " AND ".join(f"{column} = ?" for column in key_values)
        cql = f"DELETE FROM {self.keyspace}.{self.table} WHERE {conditions}"
        self._execute(cql, list(key_values.values()))


def coerce_query_values(serializer_class, raw_values):
    """Convert raw string filter values into typed values via serializer fields.

    Query-param and path values arrive as strings; each is passed through the
    matching serializer field's `to_internal_value` so ints/floats/bools/dates
    become the right Python type for Cassandra.

    Keys that don't correspond to a real serializer field are skipped, so stray
    query params can never build an invalid CQL filter.
    """
    serializer_fields = serializer_class().fields
    coerced = {}
    for column, raw_value in raw_values.items():
        field = serializer_fields.get(column)
        if field is None:
            continue
        coerced[column] = field.to_internal_value(raw_value)
    return coerced


def encode_paging_state(paging_state):
    """Serialise a driver paging-state (bytes) into a URL-safe string, or None."""
    if paging_state is None:
        return None
    return base64.urlsafe_b64encode(paging_state).decode("ascii")


def decode_paging_state(cursor):
    """Turn a client cursor string back into driver paging-state bytes, or None.

    Raises ValueError on malformed input (caller turns that into a 400).
    """
    if not cursor:
        return None
    return base64.urlsafe_b64decode(cursor.encode("ascii"))