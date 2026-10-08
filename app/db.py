"""Relational storage: local SQLite or persistent PostgreSQL in hosted functions."""
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from sqlalchemy import (Column, ForeignKey, Index, Integer, LargeBinary, MetaData,
                        String, Table, Text, CheckConstraint, UniqueConstraint,
                        create_engine, event, text)
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

metadata = MetaData()
jobs = Table('jobs', metadata,
    Column('id', String, primary_key=True), Column('course_name', Text, nullable=False),
    Column('organization', Text, nullable=False), Column('issue_date', String, nullable=False),
    Column('created_at', String, nullable=False))
certificates = Table('certificates', metadata,
    Column('id', String, primary_key=True),
    Column('job_id', String, ForeignKey('jobs.id'), nullable=False),
    Column('position', Integer, nullable=False), Column('name', Text), Column('email', Text),
    Column('status', String, nullable=False), Column('error', Text), Column('pdf', LargeBinary),
    UniqueConstraint('job_id', 'position'),
    CheckConstraint("status IN ('pending','completed','failed')"),
    CheckConstraint("(status='completed' AND pdf IS NOT NULL) OR (status!='completed' AND pdf IS NULL)"))
Index('certificates_job', certificates.c.job_id, certificates.c.position)
Index('certificates_pending', certificates.c.status)


def normalized_url(database: str):
    if '://' not in database:
        return 'sqlite:///' + database
    url = make_url(database)
    if url.drivername in ('postgres', 'postgresql'):
        return url.set(drivername='postgresql+psycopg')
    return url


@lru_cache(maxsize=32)
def engine_for(database: str):
    url = make_url(normalized_url(database))
    if url.get_backend_name() == 'sqlite':
        if url.database and url.database != ':memory:':
            Path(url.database).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(url, connect_args={'timeout': 10})
        @event.listens_for(engine, 'connect')
        def sqlite_settings(conn, _):
            conn.execute('PRAGMA foreign_keys=ON')
            conn.execute('PRAGMA journal_mode=WAL')
        return engine
    if url.get_backend_name() != 'postgresql':
        raise ValueError('Only SQLite and PostgreSQL are supported')
    # Short-lived function instances should not each maintain idle pools.
    return create_engine(url, poolclass=NullPool, hide_parameters=True,
                         connect_args={'connect_timeout': 5})


def initialize(database: str):
    engine = engine_for(database)
    with engine.begin() as conn:
        if engine.dialect.name == 'postgresql':
            # Serialize first-start schema creation across cold function instances.
            conn.execute(text('SELECT pg_advisory_xact_lock(720014603)'))
        metadata.create_all(conn)


@contextmanager
def connect(database: str, *, worker: bool = False):
    engine = engine_for(database)
    with engine.connect() as conn:
        try:
            if engine.dialect.name == 'sqlite':
                conn.exec_driver_sql('BEGIN IMMEDIATE' if worker else 'BEGIN')
            else:
                conn.begin()
                if not worker:
                    conn.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ'))
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
