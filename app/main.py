import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from threading import Event, Thread, Lock
from uuid import uuid4
from fastapi import Depends, FastAPI, HTTPException, Query, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import select, insert, func, text
from sqlalchemy.exc import SQLAlchemyError
from pydantic import ValidationError
from .db import certificates, jobs, connect, initialize, normalized_url
from sqlalchemy.engine import make_url
from .schemas import JobCreate, Recipient
from .worker import process_job, run_worker


def create_app(database: str | None = None, worker_enabled: bool | None = None):
    serverless = os.getenv('VERCEL') == '1'
    database = database or os.getenv('DATABASE_URL') or os.getenv('DATABASE_PATH', 'data/certificates.db')
    configuration_error = None
    if serverless:
        try:
            if make_url(normalized_url(database)).get_backend_name() != 'postgresql':
                configuration_error = 'Set DATABASE_URL to a persistent PostgreSQL database in Vercel, then redeploy.'
        except Exception:
            configuration_error = 'DATABASE_URL is invalid. Configure PostgreSQL in Vercel and redeploy.'
    if worker_enabled is None:
        worker_enabled = os.getenv('WORKER_ENABLED', 'true').lower() == 'true'
    # Serverless processes can stop after responding, so they must not own daemon workers.
    worker_enabled = worker_enabled and not serverless
    initialized = False
    init_lock = Lock()

    def ensure_database():
        nonlocal initialized
        if configuration_error:
            raise HTTPException(503, configuration_error)
        if not initialized:
            with init_lock:
                if not initialized:
                    try:
                        initialize(database)
                    except Exception:
                        # Connection strings can contain secrets; do not return exceptions.
                        raise HTTPException(503, 'Database unavailable. Check DATABASE_URL and database connectivity.') from None
                    initialized = True

    @asynccontextmanager
    async def lifespan(app):
        stop = Event()
        thread = None
        if not serverless:
            ensure_database()
            if worker_enabled:
                thread = Thread(target=run_worker, args=(database, stop), daemon=True)
                thread.start()
        try:
            yield
        finally:
            stop.set()
            if thread:
                thread.join(timeout=15)

    app = FastAPI(title='Bulk Certificate Generator', version='1.1.0', lifespan=lifespan,
                  description='Submit up to 1,000 recipients locally, or 100 per Vercel request, and download PDFs.')
    ready = [Depends(ensure_database)]

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=503, content={'detail': 'Database temporarily unavailable; check job status before resubmitting.'})

    @app.get('/', include_in_schema=False)
    def root():
        return RedirectResponse('/docs')

    def require_job(conn, job_id):
        row = conn.execute(select(jobs).where(jobs.c.id == job_id)).mappings().first()
        if row is None:
            raise HTTPException(404, 'Job not found')
        return row

    @app.get('/health', tags=['Health'], dependencies=ready)
    def health():
        with connect(database) as conn:
            conn.execute(text('SELECT 1'))
        return {'status': 'ok'}

    @app.post('/jobs', status_code=202, tags=['Jobs'], dependencies=ready)
    def create_job(payload: JobCreate, response: Response):
        if serverless and len(payload.recipients) > 100:
            raise HTTPException(422, 'Vercel accepts up to 100 recipients per request; split larger batches.')
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
        columns = ('id', 'job_id', 'position', 'name', 'email', 'status', 'error')
        with connect(database) as conn:
            conn.execute(insert(jobs).values(id=job_id, course_name=payload.course_name,
                organization=payload.organization, issue_date=payload.issue_date.isoformat(),
                created_at=datetime.now(timezone.utc).isoformat()))
            conn.execute(insert(certificates), [dict(zip(columns, row)) for row in records])
        if serverless:
            process_job(database, job_id)
        response.headers['Location'] = f'/jobs/{job_id}'
        failed = sum(r[5] == 'failed' for r in records)
        return {'job_id': job_id, 'total': len(records), 'accepted': len(records)-failed,
                'validation_failed': failed, 'status_url': f'/jobs/{job_id}',
                'results_url': f'/jobs/{job_id}/certificates'}

    @app.post('/jobs/{job_id}/resume', tags=['Jobs'], dependencies=ready)
    def resume_job(job_id: str):
        with connect(database) as conn:
            require_job(conn, job_id)
        process_job(database, job_id)
        return {'job_id': job_id, 'status_url': f'/jobs/{job_id}'}

    @app.get('/jobs/{job_id}', tags=['Jobs'], dependencies=ready)
    def job_status(job_id: str):
        with connect(database) as conn:
            # Keep metadata and counts in one consistent read snapshot.
            job = require_job(conn, job_id)
            counts = dict(conn.execute(select(certificates.c.status, func.count())
                .where(certificates.c.job_id == job_id).group_by(certificates.c.status)).all())
        completed, failed, pending = (counts.get(k, 0) for k in ('completed', 'failed', 'pending'))
        total = completed + failed + pending
        status = ('processing' if completed or failed else 'queued') if pending else (
            'completed' if not failed else 'failed' if not completed else 'completed_with_errors')
        return {**dict(job), 'status': status, 'total': total, 'completed': completed,
                'failed': failed, 'pending': pending,
                'progress_percent': round(100 * (completed + failed) / total, 2),
                'results_url': f'/jobs/{job_id}/certificates'}

    @app.get('/jobs/{job_id}/certificates', tags=['Certificates'], dependencies=ready)
    def list_certificates(job_id: str, limit: int = Query(100, ge=1, le=1000),
                          offset: int = Query(0, ge=0)):
        with connect(database) as conn:
            require_job(conn, job_id)
            rows = conn.execute(select(certificates.c.id, certificates.c.position, certificates.c.name,
                certificates.c.email, certificates.c.status, certificates.c.error)
                .where(certificates.c.job_id == job_id).order_by(certificates.c.position)
                .limit(limit).offset(offset)).mappings().all()
            total = conn.scalar(select(func.count()).select_from(certificates).where(certificates.c.job_id == job_id))
        items = [{**dict(row), 'download_url': f"/certificates/{row['id']}" if row['status']=='completed' else None}
                 for row in rows]
        return {'job_id': job_id, 'total': total, 'limit': limit, 'offset': offset, 'items': items}

    @app.get('/certificates/{certificate_id}', tags=['Certificates'], dependencies=ready,
             responses={200: {'content': {'application/pdf': {}}}, 409: {'description': 'Certificate not ready'}})
    def download(certificate_id: str):
        with connect(database) as conn:
            row = conn.execute(select(certificates.c.status, certificates.c.pdf)
                .where(certificates.c.id == certificate_id)).mappings().first()
        if row is None:
            raise HTTPException(404, 'Certificate not found')
        if row['status'] != 'completed':
            raise HTTPException(409, f"Certificate is {row['status']}")
        return Response(bytes(row['pdf']), media_type='application/pdf', headers={
            'Content-Disposition': f'attachment; filename="certificate-{certificate_id}.pdf"'})

    return app


app = create_app()
