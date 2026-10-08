# Bulk Certificate Generator

A FastAPI backend that accepts a batch of recipients, validates each entry, generates individual PDF certificates from one fixed template, and exposes persistent progress and downloads.

**Stack:** Python 3.12, FastAPI, SQLAlchemy, SQLite/PostgreSQL, Pydantic, ReportLab, pytest.

## Setup and run

```bash
python -m venv .venv
```

Activate the environment:

```bash
# Linux / macOS
source .venv/bin/activate

# Windows CMD
.venv\Scripts\activate
```

```bash
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000/docs for interactive Swagger documentation.
The database is created automatically at `data/certificates.db`.
An embedded background worker starts with the API; no Redis or external service is required.
Run commands from the repository root.

## Submit a batch

```bash
curl -X POST http://127.0.0.1:8000/jobs -H "Content-Type: application/json" --data-binary @examples/job.json
```

The sample contains two valid recipients and one invalid email to demonstrate partial success.
A fully valid request looks like this:

```json
{
  "course_name": "Python Backend Development",
  "organization": "Example Academy",
  "issue_date": "2026-10-08",
  "recipients": [
    {"name": "Alex Morgan", "email": "alex@example.com"},
    {"name": "Sam Taylor", "email": "sam@example.com"}
  ]
}
```

The API returns **202 Accepted** with a `Location` header:

```json
{
  "job_id": "<job-id>",
  "total": 2,
  "accepted": 2,
  "validation_failed": 0,
  "status_url": "/jobs/<job-id>",
  "results_url": "/jobs/<job-id>/certificates"
}
```

`accepted` counts valid recipients, not finished PDFs. Use the returned URLs to poll progress.

## Check progress and retrieve certificates

Replace placeholders below with actual returned IDs.

```bash
curl http://127.0.0.1:8000/jobs/JOB_ID
curl "http://127.0.0.1:8000/jobs/JOB_ID/certificates?limit=100&offset=0"
curl http://127.0.0.1:8000/certificates/CERTIFICATE_ID --output certificate.pdf
```

Job status includes `total`, `completed`, `failed`, `pending`, and `progress_percent`.
Progress means resolved recipients: `(completed + failed) / total * 100`.
Poll until `pending` is zero. The results endpoint returns a paginated `items` array, including the original zero-based `position`, status, validation/generation error, and a `download_url` for each successful certificate.

| Job status | Meaning |
| --- | --- |
| `queued` | All entries are pending |
| `processing` | Some entries are resolved and others remain pending |
| `completed` | Every certificate succeeded |
| `completed_with_errors` | Terminal batch containing successes and failures |
| `failed` | Terminal batch with no successful certificates |

A mixed batch with validation failures may immediately show `processing`; the status is derived from persisted counts, not from whether a worker currently holds a row. An all-invalid batch is accepted as an auditable job and is immediately `failed` at 100% progress.

| Method | Endpoint | Purpose |
| --- | --- | --- |
| POST | `/jobs` | Validate and queue a bulk request |
| GET | `/jobs/{job_id}` | Job metadata and progress |
| GET | `/jobs/{job_id}/certificates` | Paginated recipient outcomes |
| GET | `/certificates/{certificate_id}` | Download a PDF |
| GET | `/health` | API and database connectivity check |
| POST | `/jobs/{job_id}/resume` | Process only pending entries after an interruption |

Unknown IDs return **404**. Downloading a pending or failed certificate returns **409**. Invalid request-level metadata or pagination returns **422**.

## Validation and failure handling

- A local request must contain 1–1,000 recipients; Vercel limits requests to 100. Split larger datasets into separate jobs.
- `course_name`, `organization`, and each recipient `name` must contain 1–120 printable ASCII characters after trimming. This is an explicit scope of the predefined template with embedded Vera fonts, preventing missing glyphs. International text would require an embedded font and appropriate shaping support.
- `issue_date` is a valid calendar date in `YYYY-MM-DD` format.
- Each recipient requires a nonblank name and syntactically valid email. No email is sent and no DNS delivery check is performed. Email is metadata; the PDF displays the name, course, organization, date, and unique certificate ID.
- Unknown fields are rejected. A malformed recipient is recorded as a failed entry at its original input position; other recipients still run. Invalid raw entries are not stored, but field-level error messages are retained.
- The job and all its entries are inserted in one transaction, so submission is all-or-nothing at the database level.
- Each PDF is generated separately. Rendering exceptions are logged with the certificate ID, recorded as a safe error message, and do not stop other entries.
- Duplicate recipients are intentionally separate entries with separate certificate IDs. Retrying POST creates a new job; request idempotency is not implemented.

## Processing and storage decisions

**Local / always-on server:** the API returns 202 after persisting the batch. A worker polls pending rows every 250 ms when idle. SQLite is the zero-configuration default; set `DATABASE_URL` for PostgreSQL instead. Jobs, recipient outcomes, and PDF bytes are stored transactionally in either database.

**Vercel:** set `VERCEL=1` (provided by Vercel) and configure a persistent PostgreSQL `DATABASE_URL`. There is no daemon worker. The POST request processes its own batch before returning 202, preserving the response schema; poll the returned status URL to inspect the result. The hosted batch cap is 100 instead of 1,000 to bound invocation work. This is an intentionally synchronous serverless mode. Requests can still time out if the database or runtime is slow; for high-volume production, use a durable external queue and worker.

**Atomic generation:** each PDF and its completion status commit together. SQLite uses `BEGIN IMMEDIATE` to serialize writers. PostgreSQL uses row locking with `FOR UPDATE SKIP LOCKED`, so concurrent workers cannot process the same row. A crash before commit rolls back that recipient to pending; committed output stays intact. Status reads use a consistent transaction snapshot. PDF rendering happens inside a transaction, a deliberate simplicity tradeoff; keep batches bounded.

**Recovery:** `POST /jobs/{job_id}/resume` processes pending entries of an interrupted job without regenerating completed PDFs or retrying failed entries. It is safe to call again. For Vercel, use the same database with `python -m app.worker` on an always-on host if unattended recovery is needed. A hard timeout before the creation response can leave a stored job whose ID the client did not receive; operator lookup is needed in that case. POST creation is not idempotent, so blindly resubmitting can duplicate jobs.

**Why database PDF storage?** It keeps bytes and status atomic and avoids user-controlled filesystem paths. At large volumes, move PDFs to object storage and add an explicit recovery protocol.

**Schema setup:** tables are created lazily on the first database route in Vercel, so import/startup does not write to the read-only project directory. PostgreSQL advisory locking serializes initial schema setup. Locally, setup happens at startup. Existing SQLite files use the same tables and columns; PostgreSQL is a separate store and does not automatically import local records.

**Scope:** this assignment backend does not include authentication, per-user access control, rate limits, retention, certificate signing, or email delivery. UUIDs are identifiers, not authorization. Do not submit sensitive recipient data to a public demo. Use access controls and a request-byte limit before an Internet-facing production deployment.

## Vercel deployment and the 500 startup error

The original local-only app could fail during startup because `data/certificates.db` is inside the read-only deployment directory. Local SQLite storage also cannot provide durable shared data across Vercel function instances. Moving it to `/tmp` would not solve persistence.

This version has a root `main.py` entrypoint, a FastAPI framework preset, and PostgreSQL support. `/` redirects to `/docs`. The docs and OpenAPI schema remain available without a configured database. Database endpoints return an actionable **503** until a database is configured; this is a configuration requirement, not a working certificate service yet.

1. In Vercel, open this project and connect an existing PostgreSQL database or provision one through Storage/Marketplace using your chosen provider.
2. In **Settings → Environment Variables**, set `DATABASE_URL` to the provider's PostgreSQL connection string for Production (and Preview if needed). Use the provider's TLS-enabled URL; preserve SSL query parameters. Never commit or paste database passwords into the repository.
3. Deploy the latest commit. Use the repository root as Root Directory and FastAPI as Framework Preset. No custom output directory or build command is needed.
4. Open `/docs`, then `/health`. A configured, reachable database returns `{"status":"ok"}`.
5. POST the example batch, retrieve its status and list of certificates, then download a PDF.

The schema is created automatically; the database user needs permission to create the application's tables. `DATABASE_URL` accepts `postgres://`, `postgresql://`, or `postgresql+psycopg://` URLs. No in-memory or temporary-filesystem fallback is used on Vercel. If the site still reports `FUNCTION_INVOCATION_FAILED`, check **Vercel → Logs** for the actual import/startup traceback; this code fix cannot diagnose an unrelated platform error without those logs.

## Optional separate worker process

The default embedded worker is sufficient. To separate API and processing, set these environment variables in both terminals:

```bash
# Linux / macOS
export DATABASE_PATH=data/certificates.db
export WORKER_ENABLED=false

# Windows CMD
set DATABASE_PATH=data/certificates.db
set WORKER_ENABLED=false
```

Then run in separate terminals from the same working directory:

```bash
python -m uvicorn app.main:app
python -m app.worker
```

Use the same database path for both processes. `WORKER_ENABLED` controls only the embedded API worker. Pending work remains queued if no worker is running. The API health endpoint does not guarantee worker availability.

## Tests

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

Tests use isolated temporary databases. They cover job creation, envelope and per-recipient validation, PDF contents, progress, partial rendering failure, downloads, pagination, maximum batch size, duplicates, restart recovery, rollback after interruption, multiple workers, and the actual background worker lifecycle. GitHub Actions runs the suite with SQLite and repeats the core API tests against a dedicated PostgreSQL 16 service. Set `TEST_DATABASE_URL` only to a disposable database ending in `_test`: those integration tests drop and recreate its application tables.

## Code walkthrough

| File | Responsibility |
| --- | --- |
| `app/main.py` | API routes, application lifecycle, submission and retrieval |
| `app/schemas.py` | Request and recipient validation |
| `app/db.py` | SQLAlchemy schema, SQLite/PostgreSQL connections and transactions |
| `app/worker.py` | Durable polling queue and isolated PDF processing |
| `app/pdf.py` | Fixed certificate design with bounded text widths |
| `tests/test_api.py` | API, persistence, worker, and PDF tests |
| `examples/job.json` | Runnable mixed-success request |

For an interview walkthrough, follow `POST /jobs` → recipient validation → one database transaction → `process_one` → PDF rendering → persisted status/PDF → polling and downloading. The key tradeoff is keeping rendering and PDF storage in one transaction. SQLite serializes writes; PostgreSQL uses row locks. Vercel processing stays inside the invocation, while an always-on host can run the polling worker.
