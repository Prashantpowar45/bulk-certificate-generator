"""Deployment regression tests; no cloud credentials are required locally."""
import os
import importlib
import pytest
from fastapi.testclient import TestClient
from app.main import create_app
from app.db import initialize, connect, normalized_url
from app.worker import process_job
from sqlalchemy.engine import make_url


def data(count=1):
    return {'course_name': 'Python', 'organization': 'Academy', 'issue_date': '2026-10-08',
            'recipients': [{'name': 'Alex', 'email': 'alex@example.com'}] * count}


def test_vercel_without_database_serves_docs_and_actionable_503(monkeypatch, tmp_path):
    monkeypatch.setenv('VERCEL', '1')
    monkeypatch.delenv('DATABASE_URL', raising=False)
    monkeypatch.delenv('DATABASE_PATH', raising=False)
    monkeypatch.chdir(tmp_path)
    with TestClient(create_app()) as client:
        assert client.get('/').status_code == 200
        assert client.get('/docs').status_code == 200
        assert client.get('/openapi.json').status_code == 200
        response = client.get('/health')
        assert response.status_code == 503
        assert 'DATABASE_URL' in response.json()['detail']
        assert client.post('/jobs', json=data()).status_code == 503
    assert not (tmp_path / 'data').exists()


def test_database_setup_error_is_retryable_and_does_not_leak_credentials(monkeypatch):
    main = importlib.import_module('app.main')
    monkeypatch.setenv('VERCEL', '1')
    monkeypatch.setenv('DATABASE_URL', 'postgresql://example.invalid/db')
    calls = []
    def fail(_):
        calls.append(1)
        raise ValueError('password=secret')
    monkeypatch.setattr(main, 'initialize', fail)
    # No lifespan context: initialization also works under ASGI adapters without startup events.
    client = TestClient(create_app())
    for _ in range(2):
        response = client.get('/health')
        assert response.status_code == 503 and 'secret' not in response.text
    assert len(calls) == 2


def test_vercel_processes_in_request_without_daemon_and_enforces_cap(monkeypatch, tmp_path):
    main = importlib.import_module('app.main')
    local = str(tmp_path / 'serverless-flow.db')
    # Exercise deployment control flow locally; CI separately tests actual PostgreSQL.
    monkeypatch.setenv('VERCEL', '1')
    monkeypatch.setenv('DATABASE_URL', 'postgresql://example.invalid/db')
    monkeypatch.setattr(main, 'initialize', lambda _: initialize(local))
    monkeypatch.setattr(main, 'connect', lambda _: connect(local))
    monkeypatch.setattr(main, 'process_job', lambda _, job_id: process_job(local, job_id))
    def unexpected_worker(*args, **kwargs):
        raise AssertionError('Vercel must not create daemon workers')
    monkeypatch.setattr(main, 'Thread', unexpected_worker)
    with TestClient(create_app()) as client:
        response = client.post('/jobs', json=data())
        assert response.status_code == 202
        job = response.json()
        assert client.get(job['status_url']).json()['completed'] == 1
        url = client.get(job['results_url']).json()['items'][0]['download_url']
        assert client.get(url).content.startswith(b'%PDF')
        assert client.post('/jobs', json=data(101)).status_code == 422
        assert client.post('/jobs/' + job['job_id'] + '/resume').status_code == 200
        assert client.post('/jobs/unknown/resume').status_code == 404


def test_resume_only_processes_pending_entries(tmp_path):
    from app.worker import process_one
    database = str(tmp_path / 'resume.db')
    with TestClient(create_app(database, worker_enabled=False)) as client:
        job = client.post('/jobs', json=data(2)).json()
        process_one(database)
        url = client.get(job['results_url']).json()['items'][0]['download_url']
        original = client.get(url).content
        assert client.post('/jobs/' + job['job_id'] + '/resume').status_code == 200
        assert client.get(job['status_url']).json()['completed'] == 2
        assert client.get(url).content == original


@pytest.mark.parametrize('scheme', ['postgres', 'postgresql', 'postgresql+psycopg'])
def test_postgresql_url_uses_psycopg3(scheme):
    assert make_url(normalized_url(scheme + '://user:pass@localhost/db')).drivername == 'postgresql+psycopg'
