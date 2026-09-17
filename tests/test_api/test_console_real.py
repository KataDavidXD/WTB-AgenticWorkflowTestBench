import time
from pathlib import Path

from fastapi.testclient import TestClient
from wtb.api.console import create_app
import pytest


def wait(client, result):
    for _ in range(200):
        op = client.get('/api/v1/operations/' + result['operationId']).json()
        if op['status'] not in ('queued', 'running'):
            assert op['status'] == 'completed', op.get('error', op)
            return client.get('/api/v1/executions/' + op.get('resultExecutionId', result['executionId'])).json()
        time.sleep(0.05)
    raise AssertionError('operation timed out')


def test_real_execution_recovery(tmp_path):
    with TestClient(create_app(tmp_path)) as c:
        response = c.post('/api/v1/workflows/file-workflow/execute', json={'variantId': 'file-workflow/default', 'state': {'text': 'hello', 'repeat': 2, 'delay': 0}})
        assert response.status_code == 202, response.text
        e = wait(c, response.json())
        assert e['status'] == 'completed', e
        assert len([n for n in e['nodeRuns'] if n['nodeId'] == 'transform']) == 2
        output = Path(e['runtime']['output']) / 'report.txt'
        assert output.read_text() == 'final:pass 2:hello'
        cps = c.get(f"/api/v1/executions/{e['id']}/checkpoints").json()['items']
        cp = next(p for p in cps if p['nextNodes'] == ['transform'])
        result = c.post(f"/api/v1/executions/{e['id']}/rollback", json={'checkpointId': cp['checkpointId']})
        rolled = wait(c, result.json())
        assert rolled['status'] == 'paused'
        original = output.read_bytes()
        child = wait(c, c.post(f"/api/v1/executions/{e['id']}/branches", json={'checkpointId': cp['checkpointId'], 'state': {'text': 'child'}}).json())
        assert child['runtime']['workspace'] != e['runtime']['workspace']
        child = wait(c, c.post(f"/api/v1/executions/{child['id']}/resume", json={}).json())
        assert child['status'] == 'completed'
        assert output.read_bytes() == original
        assert 'child' in (Path(child['runtime']['output']) / 'report.txt').read_text()
    with TestClient(create_app(tmp_path)) as c:
        assert c.get('/api/v1/executions/' + e['id']).json()['status'] == 'paused'


def start(c, variant='default', state=None, breakpoints=None):
    r = c.post('/api/v1/workflows/file-workflow/execute', json={'variantId': 'file-workflow/' + variant, 'state': state or {'text': 'test', 'delay': 0}, 'breakpoints': breakpoints or []})
    assert r.status_code == 202, r.text
    return r.json()


def test_breakpoints_manual_checkpoint_edit_and_parallel(tmp_path):
    with TestClient(create_app(tmp_path)) as c:
        e = wait(c, start(c, breakpoints=['transform']))
        assert e['status'] == 'paused'
        assert e['nextNodes'] == ['transform']
        assert all(n['nodeId'] != 'transform' for n in e['nodeRuns'])
        old_cp = e['checkpointId']
        e = wait(c, c.post(f"/api/v1/executions/{e['id']}/checkpoints", json={}).json())
        assert e['checkpointId'] != old_cp
        e = wait(c, c.post(f"/api/v1/executions/{e['id']}/state", json={'state': {'text': 'edited'}}).json())
        e = wait(c, c.post(f"/api/v1/executions/{e['id']}/resume", json={}).json())
        assert e['status'] == 'completed'
        assert 'edited' in (Path(e['runtime']['output']) / 'report.txt').read_text()
        parallel = wait(c, start(c, 'parallel', {'text': 'parallel', 'delay': 0.1}))
        assert parallel['status'] == 'completed'
        nodes = {r['nodeId']: r for r in parallel['nodeRuns']}
        assert nodes['left']['step'] == nodes['right']['step']


def test_pause_stop_and_query_during_node(tmp_path):
    with TestClient(create_app(tmp_path)) as c:
        job = start(c, state={'text': 'slow', 'delay': 0.5, 'repeat': 4})
        id = job['executionId']
        for _ in range(100):
            e = c.get('/api/v1/executions/' + id).json()
            if e['activeNodes']:
                break
            time.sleep(.02)
        paused = c.post(f'/api/v1/executions/{id}/pause', json={})
        assert paused.status_code == 202
        assert c.get('/api/v1/executions/' + id).json()['status'] == 'pausing'
        e = wait(c, job)
        assert e['status'] == 'paused'
        assert len(e['nodeRuns']) == 1
        e = wait(c, c.post(f'/api/v1/executions/{id}/stop', json={}).json())
        assert e['status'] == 'cancelled'


def test_cas_failure_does_not_move_state_or_overwrite_output(tmp_path):
    with TestClient(create_app(tmp_path)) as c:
        e = wait(c, start(c))
        cps = c.get(f"/api/v1/executions/{e['id']}/checkpoints").json()['items']
        cp = cps[-1]
        files = c.get(f"/api/v1/executions/{e['id']}/checkpoints/{cp['checkpointId']}/files").json()
        digest = files[0]['hash']
        blob = Path(e['runtime']['workspace']) / '.filetrack' / 'blobs' / digest[:2] / digest[2:]
        blob.write_bytes(b'corrupt')
        original = (Path(e['runtime']['output']) / 'report.txt').read_bytes()
        job = c.post(f"/api/v1/executions/{e['id']}/rollback", json={'checkpointId': cp['checkpointId']}).json()
        for _ in range(100):
            op = c.get('/api/v1/operations/' + job['operationId']).json()
            if op['status'] == 'failed':
                break
            time.sleep(.03)
        assert op['status'] == 'failed'
        actual = c.get('/api/v1/executions/' + e['id']).json()
        assert actual['checkpointId'] == e['checkpointId']
        assert actual['state'] == e['state']
        assert (Path(e['runtime']['output']) / 'report.txt').read_bytes() == original
        assert c.get(f"/api/v1/executions/{e['id']}/checkpoints/{cp['checkpointId']}/file", params={'path': '../../secret'}).status_code == 404


def test_batch_limit_failure_isolation_and_variant(tmp_path):
    with TestClient(create_app(tmp_path)) as c:
        response = c.post('/api/v1/batch-tests', json={'variants': ['file-workflow/transform:uppercase'], 'inputs': [{'text': 'first', 'delay': .2}, {'text': 'bad', 'fail': True, 'delay': .2}, {'text': 'third', 'delay': .2}]})
        assert response.status_code == 202
        batch = response.json()
        for _ in range(200):
            items = c.get(f"/api/v1/batch-tests/{batch['id']}/results").json()['items']
            assert len([e for e in items if e['status'] == 'running']) <= 2
            if all(e['status'] in ['failed', 'completed'] for e in items):
                break
            time.sleep(.03)
        assert [e['status'] for e in items].count('completed') == 2
        first = items[0]
        assert 'FIRST' in (Path(first['runtime']['output']) / 'report.txt').read_text()
        assert len(c.get('/api/v1/audit/events').json()['items']) > 0
        assert c.get('/api/v1/system').json()['outbox'] is None


def test_restart_marks_orphans_failed(tmp_path):
    from wtb.application.services.console_service import ConsoleService
    service = ConsoleService(tmp_path)
    service.store.put('execution', {'id': 'orphan', 'status': 'running'})
    service.close()
    with TestClient(create_app(tmp_path)) as c:
        assert c.get('/api/v1/executions/orphan').json()['status'] == 'failed'


def test_websocket_and_catalog_empty_config(tmp_path):
    with TestClient(create_app(tmp_path)) as c:
        with c.websocket_connect('/ws') as ws:
            result = start(c)
            event = ws.receive_json()
            assert event['executionId'] == result['executionId']
            assert event['seq'] > 0
        wait(c, result)
    config = tmp_path / 'empty.json'
    config.write_text('{"projects": []}')
    with TestClient(create_app(tmp_path / 'empty', config)) as c:
        assert c.get('/api/v1/catalog').json()['projects'] == []


def test_catalog_exposes_serializable_workflow_project_sdk_metadata_and_filters(tmp_path):
    with TestClient(create_app(tmp_path)) as c:
        catalog = c.get('/api/v1/catalog').json()
        project = catalog['projects'][0]
        assert project['name'] == 'file-workflow'
        assert project['version'] == 1
        assert project['sdk']['execution']['batch_executor'] == 'threadpool'
        assert project['sdk']['pauseStrategy']['mode'] == 'before_node'
        assert project['nodeVariantDetails']['transform'][0]['name'] == 'uppercase'
        assert project['workflowVariantDetails']['parallel']['name'] == 'parallel'
        assert catalog['capabilities']['local'] is True
        assert catalog['capabilities']['ray'] is False

        result = start(c)
        execution = wait(c, result)
        page = c.get('/api/v1/executions', params={'projectId': 'file-workflow', 'variantId': execution['variantId']}).json()
        assert page['total'] == 1
        assert page['items'][0]['id'] == execution['id']
