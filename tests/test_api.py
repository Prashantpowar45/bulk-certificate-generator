from concurrent.futures import ThreadPoolExecutor
import os
from sqlalchemy.engine import make_url
from io import BytesIO
from time import monotonic, sleep
import pytest
from sqlalchemy import text
from fastapi.testclient import TestClient
from pypdf import PdfReader
from app.db import connect, engine_for, metadata, normalized_url
from app.main import create_app
from app.worker import process_one
import app.worker as worker


@pytest.fixture
def setup(tmp_path):
    database = os.getenv('TEST_DATABASE_URL') or str(tmp_path / 'test.db')
    if os.getenv('TEST_DATABASE_URL'):
        assert make_url(normalized_url(database)).database.endswith('_test'), 'Use a dedicated *_test database'
        metadata.drop_all(engine_for(database))
    with TestClient(create_app(database, worker_enabled=False)) as client:
        yield client, database


def payload(recipients=None):
    return {'course_name': 'FastAPI Fundamentals', 'organization': 'Example Academy',
            'issue_date': '2026-10-08', 'recipients': recipients if recipients is not None else [
                {'name': 'Alex Morgan', 'email': 'alex@example.com'},
                {'name': 'Sam Taylor', 'email': 'sam@example.com'}]}


def submit(client, data=None):
    response = client.post('/jobs', json=data or payload())
    assert response.status_code == 202
    assert response.headers['location'] == response.json()['status_url']
    return response.json()


def drain(database):
    while process_one(database):
        pass


def test_create_progress_generate_and_download(setup):
    client, database = setup
    job = submit(client)
    status = client.get(job['status_url']).json()
    assert (status['status'], status['pending'], status['progress_percent']) == ('queued', 2, 0)
    items = client.get(job['results_url']).json()['items']
    assert client.get('/certificates/' + items[0]['id']).status_code == 409
    assert process_one(database)
    status = client.get(job['status_url']).json()
    assert (status['status'], status['completed'], status['progress_percent']) == ('processing', 1, 50)
    drain(database)
    status = client.get(job['status_url']).json()
    assert (status['status'], status['completed'], status['progress_percent']) == ('completed', 2, 100)
    items = client.get(job['results_url']).json()['items']
    for item in items:
        response = client.get(item['download_url'])
        assert response.status_code == 200
        assert response.headers['content-type'] == 'application/pdf'
        assert 'attachment;' in response.headers['content-disposition']
        assert response.content.startswith(b'%PDF')
        pdf = PdfReader(BytesIO(response.content))
        assert len(pdf.pages) == 1
        text = pdf.pages[0].extract_text()
        for value in (item['name'], 'FastAPI Fundamentals', 'Example Academy', '2026-10-08', item['id']):
            assert value in text


@pytest.mark.parametrize('changes', [
    {'recipients': []}, {'recipients': [{}] * 1001}, {'issue_date': 'bad'},
    {'course_name': ''}, {'organization': '   '}, {'unknown': 'field'},
    {'course_name': 'x' * 121}, {'recipients': None}, {'organization': 'Bad\ntext'}
])
def test_invalid_envelope_returns_422_without_creating_job(setup, changes):
    client, database = setup
    data = payload() | changes
    assert client.post('/jobs', json=data).status_code == 422
    with connect(database) as conn:
        assert conn.execute(text('SELECT COUNT(*) FROM jobs')).fetchone()[0] == 0


@pytest.mark.parametrize('invalid', [
    {'name': '', 'email': 'a@example.com'}, {'name': 'Alex', 'email': 'bad'},
    {'name': 'Alex'}, None, 'not an object', 42,
    {'name': 'Alex', 'email': 'a@example.com', 'unexpected': True},
    {'name': 'नाम', 'email': 'a@example.com'},
    {'name': 'a\nb', 'email': 'a@example.com'}
])
def test_invalid_recipient_does_not_block_valid_one(setup, invalid):
    client, database = setup
    job = submit(client, payload([invalid, {'name': 'Valid Person', 'email': 'valid@example.com'}]))
    assert (job['accepted'], job['validation_failed']) == (1, 1)
    drain(database)
    status = client.get(job['status_url']).json()
    assert (status['status'], status['failed'], status['completed']) == ('completed_with_errors', 1, 1)
    items = client.get(job['results_url']).json()['items']
    assert items[0]['position'] == 0 and items[0]['error'] and items[0]['download_url'] is None
    assert client.get('/certificates/' + items[0]['id']).status_code == 409
    assert client.get(items[1]['download_url']).status_code == 200


def test_all_invalid_job_is_terminal(setup):
    client, database = setup
    job = submit(client, payload([{}, None]))
    assert not process_one(database)
    status = client.get(job['status_url']).json()
    assert status['status'] == 'failed' and status['progress_percent'] == 100


def test_individual_generation_failure_does_not_stop_batch(setup, monkeypatch):
    client, database = setup
    original = worker.render_certificate
    def selective_failure(name, *args):
        if name == 'Alex Morgan':
            raise RuntimeError('private internal details')
        return original(name, *args)
    monkeypatch.setattr(worker, 'render_certificate', selective_failure)
    job = submit(client)
    drain(database)
    status = client.get(job['status_url']).json()
    assert status['status'] == 'completed_with_errors'
    items = client.get(job['results_url']).json()['items']
    assert items[0]['status'] == 'failed' and 'private internal details' not in items[0]['error']
    assert client.get(items[1]['download_url']).status_code == 200


def test_pending_work_survives_restart_and_downloads_persist(setup):
    client, database = setup
    job = submit(client)
    process_one(database)
    with TestClient(create_app(database, worker_enabled=False)) as restarted:
        drain(database)
        assert restarted.get(job['status_url']).json()['completed'] == 2
        url = restarted.get(job['results_url']).json()['items'][0]['download_url']
        assert restarted.get(url).status_code == 200


def test_interrupted_transaction_rolls_back_and_can_retry(setup, monkeypatch):
    client, database = setup
    job = submit(client)
    original = worker.render_certificate
    def interrupt(*args):
        raise KeyboardInterrupt()
    monkeypatch.setattr(worker, 'render_certificate', interrupt)
    with pytest.raises(KeyboardInterrupt):
        process_one(database)
    assert client.get(job['status_url']).json()['pending'] == 2
    monkeypatch.setattr(worker, 'render_certificate', original)
    drain(database)
    assert client.get(job['status_url']).json()['completed'] == 2


def test_multiple_workers_do_not_duplicate_generation(setup, monkeypatch):
    client, database = setup
    original = worker.render_certificate
    calls = []
    def record(*args):
        calls.append(args[-1])
        return original(*args)
    monkeypatch.setattr(worker, 'render_certificate', record)
    job = submit(client)
    with ThreadPoolExecutor(max_workers=3) as executor:
        list(executor.map(lambda _: drain(database), range(3)))
    assert len(calls) == len(set(calls)) == 2
    assert client.get(job['status_url']).json()['completed'] == 2


def test_background_worker_end_to_end(tmp_path):
    with TestClient(create_app(str(tmp_path / 'background.db'), worker_enabled=True)) as client:
        job = submit(client)
        deadline = monotonic() + 5
        while monotonic() < deadline:
            status = client.get(job['status_url']).json()
            if status['pending'] == 0:
                break
            sleep(0.02)
        assert status['status'] == 'completed'


def test_pagination_unknown_ids_and_health(setup):
    client, _ = setup
    job = submit(client)
    page = client.get(job['results_url'], params={'limit': 1, 'offset': 1}).json()
    assert page['total'] == 2 and len(page['items']) == 1 and page['items'][0]['position'] == 1
    assert client.get(job['results_url'], params={'limit': 0}).status_code == 422
    assert client.get(job['results_url'], params={'offset': -1}).status_code == 422
    for url in ['/jobs/unknown', '/jobs/unknown/certificates', '/certificates/unknown']:
        assert client.get(url).status_code == 404
    assert client.get('/health').json() == {'status': 'ok'}


def test_maximum_batch_and_duplicate_recipients(setup):
    client, database = setup
    job = submit(client, payload([{'name': 'Same Person', 'email': 'same@example.com'}] * 1000))
    assert job['accepted'] == 1000
    items = client.get(job['results_url'], params={'limit': 1000}).json()['items']
    assert len({i['id'] for i in items}) == 1000
    # Drain a separate small batch elsewhere; this tests the actual request cap.


def test_long_fields_generate_single_page(setup):
    client, database = setup
    data = payload([{'name': 'A' * 120, 'email': 'a@example.com'}])
    data.update(course_name='C' * 120, organization='O' * 120)
    job = submit(client, data)
    drain(database)
    url = client.get(job['results_url']).json()['items'][0]['download_url']
    pdf = PdfReader(BytesIO(client.get(url).content))
    assert len(pdf.pages) == 1
    assert 'A' * 120 in pdf.pages[0].extract_text()
