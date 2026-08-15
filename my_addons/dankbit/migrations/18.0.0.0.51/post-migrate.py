# -*- coding: utf-8 -*-
"""Index dankbit_trade's hot query columns (name/deribit_ts/expiration).

Every trade-window query in this addon (_compute_asset, chart_png_zones,
the delta-zero endpoints, the Signal Bot, ...) filters on
`name ILIKE '<asset>-%'` combined with deribit_ts/expiration ranges. With
~4M rows and no index beyond the PK/deribit_trade_identifier uniqueness
index, these were full sequential scans (measured ~750ms-2s per call,
repeated several times per Delta Chart page load/poll).

CREATE INDEX CONCURRENTLY is used throughout so building these doesn't take
a lock that would stall the WS ingestion service's own inserts.
CONCURRENTLY cannot run inside a transaction block, so the connection is
switched to autocommit for the duration of this migration and restored
afterward.
"""
import logging

_logger = logging.getLogger(__name__)

_STATEMENTS = [
    "CREATE EXTENSION IF NOT EXISTS pg_trgm",
    # name is matched via ILIKE '<asset>-%' everywhere (both raw SQL and the
    # ORM's "=ilike" domain operator) — a trigram GIN index supports ILIKE
    # directly, with no query rewrite needed, unlike a plain btree which
    # can't be used for a case-insensitive match.
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS dankbit_trade_name_trgm_idx "
    "ON dankbit_trade USING gin (name gin_trgm_ops)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS dankbit_trade_deribit_ts_idx "
    "ON dankbit_trade (deribit_ts)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS dankbit_trade_expiration_idx "
    "ON dankbit_trade (expiration)",
]


def migrate(cr, version):
    cr.commit()
    old_isolation_level = cr._cnx.isolation_level
    cr._cnx.set_isolation_level(0)  # psycopg2 ISOLATION_LEVEL_AUTOCOMMIT
    try:
        for stmt in _STATEMENTS:
            _logger.info("dankbit 18.0.0.0.51 migration: %s", stmt)
            cr.execute(stmt)
    finally:
        cr._cnx.set_isolation_level(old_isolation_level)
