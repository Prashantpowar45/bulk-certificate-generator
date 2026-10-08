"""Process durable rows, independently committing each generated certificate."""
import logging
import os
from threading import Event
from sqlalchemy import select, update
from .db import certificates, jobs, connect, initialize
from .pdf import render_certificate

logger = logging.getLogger(__name__)


def process_one(database: str, job_id: str | None = None) -> bool:
    with connect(database, worker=True) as conn:
        query = (select(certificates.c.id, certificates.c.name, jobs.c.course_name,
                        jobs.c.organization, jobs.c.issue_date)
                 .join(jobs, jobs.c.id == certificates.c.job_id)
                 .where(certificates.c.status == 'pending')
                 .order_by(jobs.c.created_at, certificates.c.job_id, certificates.c.position).limit(1))
        if job_id is not None:
            query = query.where(certificates.c.job_id == job_id)
        if conn.dialect.name == 'postgresql':
            query = query.with_for_update(skip_locked=True, of=certificates)
        row = conn.execute(query).mappings().first()
        if row is None:
            return False
        try:
            pdf = render_certificate(row['name'], row['course_name'], row['organization'],
                                     row['issue_date'], row['id'])
            values = {'status': 'completed', 'pdf': pdf}
        except Exception:
            logger.exception('Certificate generation failed: %s', row['id'])
            values = {'status': 'failed', 'error':
                      'PDF generation failed; contact the operator with the certificate ID.'}
        conn.execute(update(certificates).where(certificates.c.id == row['id']).values(**values))
    return True


def process_job(database: str, job_id: str):
    """Run while the HTTP invocation is active; never rely on a daemon in Vercel."""
    while process_one(database, job_id):
        pass


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
    database = os.getenv('DATABASE_URL') or os.getenv('DATABASE_PATH', 'data/certificates.db')
    initialize(database)
    try:
        run_worker(database, Event())
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
