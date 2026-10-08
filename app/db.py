"""Short-lived SQLite connections shared by API and worker processes."""
import sqlite3
from contextlib import contextmanager
from pathlib import Path


def initialize(path: str):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as conn:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.executescript('''
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            course_name TEXT NOT NULL,
            organization TEXT NOT NULL,
            issue_date TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS certificates (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL REFERENCES jobs(id),
            position INTEGER NOT NULL,
            name TEXT,
            email TEXT,
            status TEXT NOT NULL CHECK(status IN ('pending','completed','failed')),
            error TEXT,
            pdf BLOB,
            UNIQUE(job_id, position),
            CHECK((status='completed' AND pdf IS NOT NULL) OR
                  (status!='completed' AND pdf IS NULL))
        );
        CREATE INDEX IF NOT EXISTS certificates_job ON certificates(job_id, position);
        CREATE INDEX IF NOT EXISTS certificates_pending ON certificates(status);
        ''')


@contextmanager
def connect(path: str):
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
