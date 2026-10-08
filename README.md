# Bulk Certificate Generator

A FastAPI backend that accepts a batch of recipients, validates each entry, generates individual PDF certificates from one fixed template, and exposes persistent progress and downloads.

**Stack:** Python 3.12, FastAPI, SQLite, Pydantic, ReportLab, pytest.

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
| GET | `/health` | Basic API and database connectivity check |

Unknown IDs return **404**. Downloading a pending or failed certificate returns **409**. Invalid request-level metadata or pagination returns **422**.

## Validation and failure handling

- A request must contain 1–1,000 recipients; split larger datasets into separate jobs.
- `course_name`, `organization`, and each recipient `name` must contain 1–120 printable ASCII characters after trimming. This is an explicit scope of the predefined template with embedded Vera fonts, preventing missing glyphs. International text would require an embedded font and appropriate shaping support.
- `issue_date` is a valid calendar date in `YYYY-MM-DD` format.
- Each recipient requires a nonblank name and syntactically valid email. No email is sent and no DNS delivery check is performed. Email is metadata; the PDF displays the name, course, organization, date, and unique certificate ID.
- Unknown fields are rejected. A malformed recipient is recorded as a failed entry at its original input position; other recipients still run. Invalid raw entries are not stored, but field-level error messages are retained.
- The job and all its entries are inserted in one transaction, so submission is all-or-nothing at the database level.
- Each PDF is generated separately. Rendering exceptions are logged with the certificate ID, recorded as a safe error message, and do not stop other entries.
- Duplicate recipients are intentionally separate entries with separate certificate IDs. Retrying POST creates a new job; request idempotency is not implemented.

## Processing and storage decisions

**Why background processing?** Returning 202 keeps bulk PDF generation out of the request path. HTTP requests validate and persist work; a worker polls the durable pending queue every 250 ms when idle.

**Why SQLite?** It is a relational database with foreign keys and transactions and needs no external setup for this assignment. Jobs and certificates have a one-to-many relationship. Pending work and output PDFs survive restarts.

**Atomic generation:** the worker opens `BEGIN IMMEDIATE`, selects one pending row, renders the PDF, saves the PDF bytes and status, and commits. A crash before commit rolls everything back, leaving the entry pending for recovery. The system can render again after an interrupted attempt, but only one committed PDF exists per certificate. Multiple workers are safe because SQLite serializes writers. This deliberately holds the write lock during one small PDF render; workers do not generate in parallel. WAL mode allows status reads during generation. Database operations wait up to 10 seconds for a writer lock.

**Why store PDF bytes in the database?** For this bounded assignment, it keeps PDF output and completion status in the same atomic transaction and avoids inconsistent file paths or missing disk files. No user-provided value becomes a filesystem path. At much larger volumes, use PostgreSQL, a job queue, and object storage with explicit recovery and retry policies.

**Progress:** computed from certificate rows rather than mutable counters, so counters cannot drift after failures or restarts. List responses omit PDF blobs and support pagination; downloads fetch only one PDF.

**Scope:** this is a local assignment backend, without authentication, per-user access control, rate limits, automatic retention, certificate signing, or email delivery. UUIDs identify records but are not authorization. Add these controls before exposing recipient data publicly. The recipient cap bounds batch size, but a reverse proxy should enforce a request-byte limit for an Internet-facing deployment. No retry endpoint is included; corrected or intentionally retried entries can be submitted as a new job.

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

Tests use isolated temporary databases. They cover job creation, envelope and per-recipient validation, PDF contents, progress, partial rendering failure, downloads, pagination, maximum batch size, duplicates, restart recovery, rollback after interruption, multiple workers, and the actual background worker lifecycle. GitHub Actions runs the same suite on push and pull requests.

## Code walkthrough

| File | Responsibility |
| --- | --- |
| `app/main.py` | API routes, application lifecycle, submission and retrieval |
| `app/schemas.py` | Request and recipient validation |
| `app/db.py` | SQLite schema, connections, transactions |
| `app/worker.py` | Durable polling queue and isolated PDF processing |
| `app/pdf.py` | Fixed certificate design with bounded text widths |
| `tests/test_api.py` | API, persistence, worker, and PDF tests |
| `examples/job.json` | Runnable mixed-success request |

For an interview walkthrough, follow `POST /jobs` → recipient validation → one database transaction → `process_one` → PDF rendering → persisted status/PDF → polling and downloading. The central tradeoff is a simple, crash-safe SQLite transaction per certificate at the cost of serialized generation. A changed requirement for high parallel throughput would call for a different claiming mechanism and external PDF storage.
