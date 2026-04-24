"""
simulation/connections.py — Database connection setup.

Creates PostgreSQL and ClickHouse connections from environment variables.
"""

import os
from collections import namedtuple

import clickhouse_connect
import psycopg2
import sqlalchemy

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

Connections = namedtuple('Connections', ['pg_conn', 'pg_engine', 'ch'])


def connect() -> Connections:
    pg_conn = psycopg2.connect(
        host=os.environ['PG_HOST'],
        port=os.environ.get('PG_PORT', 5432),
        dbname=os.environ['PG_DB'],
        user=os.environ['PG_USER'],
        password=os.environ['PG_PASSWORD'],
        sslmode=os.environ.get('PG_SSLMODE', 'prefer'),
    )

    _pg_url = sqlalchemy.engine.URL.create(
        drivername='postgresql+psycopg2',
        host=os.environ['PG_HOST'],
        port=int(os.environ.get('PG_PORT', 5432)),
        database=os.environ['PG_DB'],
        username=os.environ['PG_USER'],
        password=os.environ['PG_PASSWORD'],
        query={'sslmode': os.environ.get('PG_SSLMODE', 'prefer')},
    )
    pg_engine = sqlalchemy.create_engine(_pg_url)

    ch = clickhouse_connect.get_client(
        host=os.environ['CH_HOST'],
        port=int(os.environ.get('CH_PORT', 8123)),
        database=os.environ['CH_DB'],
        username=os.environ['CH_USER'],
        password=os.environ['CH_PASSWORD'],
        verify=False,
    )

    return Connections(pg_conn=pg_conn, pg_engine=pg_engine, ch=ch)
