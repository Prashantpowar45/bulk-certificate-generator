"""Durable database queue. A transaction atomically renders and saves one PDF."""
import logging
import os
from threading import Event
from .db import connect, initialize
from .pdf import render_certificate

logger = logging.getLogger(__name__)


def process_one(database: str) -> bool:
    with connect(database) as conn:
        # SQLite serializes writers. Other workers cannot claim this row;
        # a crash rolls it back to pending, including the PDF and status.
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('''SELECT c.id, c.name, j.course_name, j.organization,
            j.issue_date FROM certificates c JOIN jobs j ON j.id=c.job_id
            WHERE c.status='pending' ORDER BY c.rowid LIMIT 1''').fetchone()
        if row is None:
            return False
        try:
            pdf = render_certificate(row['name'], row['course_name'],
                                     row['organization'], row['issue_date'], row['id'])
            conn.execute("UPDATE certificates SET status='completed', pdf=? WHERE id=?",
                         (pdf, row['id']))
        except Exception:
            logger.exception('Certificate generation failed: %s', row['id'])
            conn.execute("UPDATE certificates SET status='failed', error=? WHERE id=?",
                         ('PDF generation failed; contact the operator with the certificate ID.', row['id']))
    return True


def run_worker(database: str, stop: Event, interval: float = 0.25):
    while not stop.is_set():
        try:
            if process_one(database):
                continue
        except Exception:
            logger.exception('Worker iteration failed; retrying after a short delay')
        stop.wait(interval)


def main():
    logging.basicConfig(level=logging.INFO)
    database = os.getenv('DATABASE_PATH', 'data/certificates.db')
    initialize(database)
    try:
        run_worker(database, Event())
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
