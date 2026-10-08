import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from threading import Event, Thread
from uuid import uuid4
from fastapi import FastAPI, HTTPException, Query, Response
from pydantic import ValidationError
from .db import connect, initialize
from .schemas import JobCreate, Recipient
from .worker import run_worker


def create_app(database: str | None = None, worker_enabled: bool | None = None):
    database = database or os.getenv('DATABASE_PATH', 'data/certificates.db')
    if worker_enabled is None:
        worker_enabled = os.getenv('WORKER_ENABLED', 'true').lower() == 'true'

    @asynccontextmanager
    async def lifespan(app):
        initialize(database)
        stop = Event()
        thread = None
        if worker_enabled:
            thread = Thread(target=run_worker, args=(database, stop), daemon=True)
            thread.start()
        yield
        stop.set()
        if thread:
            thread.join(timeout=15)

    app = FastAPI(title='Bulk Certificate Generator', version='1.0.0', lifespan=lifespan,
                  description='Submit up to 1,000 recipients, poll progress, and download PDFs.')

    def require_job(conn, job_id):
        row = conn.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if row is None:
            raise HTTPException(404, 'Job not found')
        return row

    @app.get('/health', tags=['Health'])
    def health():
        with connect(database) as conn:
            conn.execute('SELECT 1')
        return {'status': 'ok'}

    @app.post('/jobs', status_code=202, tags=['Jobs'])
    def create_job(payload: JobCreate, response: Response):
        job_id = str(uuid4())
        records = []
        for position, raw in enumerate(payload.recipients):
            certificate_id = str(uuid4())
            try:
                recipient = Recipient.model_validate(raw)
                records.append((certificate_id, job_id, position, recipient.name,
                                str(recipient.email), 'pending', None))
            except ValidationError as exc:
                errors = '; '.join(f"{'.'.join(map(str, e['loc'])) or 'recipient'}: {e['msg']}"
                                   for e in exc.errors(include_input=False, include_url=False))
                records.append((certificate_id, job_id, position, None, None, 'failed', errors))
        with connect(database) as conn:
            conn.execute('INSERT INTO jobs VALUES (?, ?, ?, ?, ?)',
                         (job_id, payload.course_name, payload.organization,
                          payload.issue_date.isoformat(), datetime.now(timezone.utc).isoformat()))
            conn.executemany('''INSERT INTO certificates
                (id, job_id, position, name, email, status, error) VALUES (?, ?, ?, ?, ?, ?, ?)''', records)
        response.headers['Location'] = f'/jobs/{job_id}'
        failed = sum(r[5] == 'failed' for r in records)
        return {'job_id': job_id, 'total': len(records), 'accepted': len(records)-failed,
                'validation_failed': failed, 'status_url': f'/jobs/{job_id}',
                'results_url': f'/jobs/{job_id}/certificates'}

    @app.get('/jobs/{job_id}', tags=['Jobs'])
    def job_status(job_id: str):
        with connect(database) as conn:
            # Keep metadata and counts in one consistent read snapshot.
            conn.execute('BEGIN')
            job = require_job(conn, job_id)
            counts = dict(conn.execute('SELECT status, COUNT(*) FROM certificates WHERE job_id=? GROUP BY status',
                                       (job_id,)).fetchall())
        completed, failed, pending = (counts.get(k, 0) for k in ('completed', 'failed', 'pending'))
        total = completed + failed + pending
        status = ('processing' if completed or failed else 'queued') if pending else (
            'completed' if not failed else 'failed' if not completed else 'completed_with_errors')
        return {**dict(job), 'status': status, 'total': total, 'completed': completed,
                'failed': failed, 'pending': pending,
                'progress_percent': round(100 * (completed + failed) / total, 2),
                'results_url': f'/jobs/{job_id}/certificates'}

    @app.get('/jobs/{job_id}/certificates', tags=['Certificates'])
    def list_certificates(job_id: str, limit: int = Query(100, ge=1, le=1000),
                          offset: int = Query(0, ge=0)):
        with connect(database) as conn:
            conn.execute('BEGIN')
            require_job(conn, job_id)
            rows = conn.execute('''SELECT id, position, name, email, status, error FROM certificates
                WHERE job_id=? ORDER BY position LIMIT ? OFFSET ?''', (job_id, limit, offset)).fetchall()
            total = conn.execute('SELECT COUNT(*) FROM certificates WHERE job_id=?', (job_id,)).fetchone()[0]
        items = [{**dict(row), 'download_url': f"/certificates/{row['id']}" if row['status']=='completed' else None}
                 for row in rows]
        return {'job_id': job_id, 'total': total, 'limit': limit, 'offset': offset, 'items': items}

    @app.get('/certificates/{certificate_id}', tags=['Certificates'],
             responses={200: {'content': {'application/pdf': {}}}, 409: {'description': 'Certificate not ready'}})
    def download(certificate_id: str):
        with connect(database) as conn:
            row = conn.execute('SELECT status, pdf FROM certificates WHERE id=?', (certificate_id,)).fetchone()
        if row is None:
            raise HTTPException(404, 'Certificate not found')
        if row['status'] != 'completed':
            raise HTTPException(409, f"Certificate is {row['status']}")
        return Response(bytes(row['pdf']), media_type='application/pdf', headers={
            'Content-Disposition': f'attachment; filename="certificate-{certificate_id}.pdf"'})

    return app


app = create_app()
