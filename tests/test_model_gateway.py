"""逐次外发同意、落账预算、无队列及返回后二次核验；仅模拟 HTTP。"""
from __future__ import annotations

import json
import uuid

import httpx
import pytest
from test_deepseek_model import envelope
from test_model_tagging import add_image
from test_model_tagging import model_data as model_data

from pc.adapters.deepseek_model import DeepSeekModel
from pc.services.model_access import ModelAccess


@pytest.fixture
def gateway_data(model_data):
    from pc.services.model_gateway import ModelGateway
    conn, ids, tags, source = model_data
    now = [0.0]
    sessions = {'session'}
    access = ModelAccess(clock=lambda: now[0])
    access.set_key('session', 'TEST-ONLY-GATEWAY-KEY')
    access.set_access(conn, 'session', True, confirmed=True)
    calls = []
    behavior = [lambda _: httpx.Response(200, json=envelope())]
    def handle(request):
        assert not conn.in_transaction, '网络期间禁止持有 SQLite 事务'
        row = conn.execute('SELECT * FROM egress_log ORDER BY created_at DESC LIMIT 1').fetchone()
        assert row['status'] == 'dispatching' and row['dispatched_at'] is not None
        calls.append(request)
        return behavior[0](request)
    gateway = ModelGateway(access, session_valid=lambda s: s in sessions, provider_factory=lambda key: DeepSeekModel(key, _transport=httpx.MockTransport(handle)))
    return conn, ids, tags, source, access, gateway, calls, behavior, now, sessions


def preview(data):
    conn, ids, _, _, _, gateway, *_ = data
    return gateway.preview(conn, 'session', content_id=ids[0])


def test_preview_is_only_metadata_and_requires_explicit_per_send_consent(gateway_data):
    conn, _, _, _, _, gateway, calls, *_ = gateway_data
    p = preview(gateway_data)
    assert p['text_bytes'] == 9 and p['image_count'] == 0 and p['candidate_count'] == 2
    assert p['parts'][0]['text'] == '正文一'
    assert p['attempts_remaining'] == 20 and p['max_output_tokens'] == 2048
    assert p['metadata_warning'] and p['unknown_outcome_warning']
    assert 'PRIVATE' not in json.dumps(p) and 'TEST-ONLY-GATEWAY-KEY' not in json.dumps(p)
    assert calls == [] and conn.execute('SELECT count(*) FROM egress_log').fetchone()[0] == 0
    with pytest.raises(ValueError, match='确认'):
        gateway.send(conn, 'session', p['preview_id'])
    assert calls == []


def test_preview_uses_runtime_pixel_policy(gateway_data, tmp_path):
    conn, ids, _, _, access, _gateway, calls, *_ = gateway_data
    media = add_image(conn, ids[0], tmp_path / 'media', b'\x89PNG\r\n\x1a\noriginal')
    conn.execute('UPDATE media SET width=10,height=10 WHERE media_id=?', (media,))
    access.max_image_pixels = 99
    with pytest.raises(ValueError, match='像素'):
        preview(gateway_data)
    assert calls == []


def test_success_is_suggested_only_and_replay_never_dispatches(gateway_data):
    conn, _, _, _, _, gateway, calls, *_ = gateway_data
    p = preview(gateway_data)
    result = gateway.send(conn, 'session', p['preview_id'], confirmed=True)
    assert result['status'] == 'succeeded' and result['run_id'] > 0
    assert len(calls) == 1
    assert conn.execute("SELECT count(*) FROM tag_assignment WHERE source='human'").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM tag_assignment WHERE source='suggested'").fetchone()[0] == 1
    assert conn.execute('SELECT count(*) FROM tag').fetchone()[0] == 2
    log = dict(conn.execute('SELECT * FROM egress_log').fetchone())
    assert log['status'] == 'succeeded' and log['run_id'] == result['run_id']
    assert json.loads(log['usage']) == {'prompt_tokens': 10, 'completion_tokens': 8}
    assert all(secret not in json.dumps(log) for secret in ['PRIVATE', '正文一', 'TEST-ONLY-GATEWAY-KEY'])
    replay = gateway.send(conn, 'session', p['preview_id'], confirmed=True)
    assert replay['request_id'] == result['request_id'] and replay['status'] == 'succeeded'
    assert 'run_id' not in replay and len(calls) == 1


@pytest.mark.parametrize('change', ['catalog', 'body', 'authorization', 'policy', 'key', 'session', 'expiry'])
def test_changed_preview_requires_new_consent_without_attempt(gateway_data, change, monkeypatch):
    conn, _, _, _, access, gateway, calls, _, now, sessions = gateway_data
    p = preview(gateway_data)
    if change == 'catalog':
        conn.execute("INSERT INTO tag(uuid,name,name_norm,namespace) VALUES (?,'新增','新增','general')", (str(uuid.uuid4()),))
    elif change == 'body':
        conn.execute("UPDATE content_item SET text_content='修改'")
    elif change == 'authorization':
        conn.execute("UPDATE source_authorization SET revoked_at='gone'")
    elif change == 'policy':
        monkeypatch.setenv('PC__PRIVACY__LOCAL_ONLY_MODE', 'true')
    elif change == 'key':
        access.set_key('session', 'NEW-TEST-ONLY-KEY')
    elif change == 'session':
        sessions.clear()
    else:
        now[0] = 300.0
    with pytest.raises(ValueError):
        gateway.send(conn, 'session', p['preview_id'], confirmed=True)
    assert calls == [] and conn.execute('SELECT count(*) FROM egress_log').fetchone()[0] == 0


@pytest.mark.parametrize('change', ['body', 'authorization', 'selected_tag', 'key', 'session'])
def test_invalidated_inflight_response_is_not_confirmable(gateway_data, change):
    conn, _, _, _, access, gateway, calls, behavior, _, sessions = gateway_data
    p = preview(gateway_data)
    def invalidate(_request):
        if change == 'body':
            conn.execute("UPDATE content_item SET text_content='改动'")
        elif change == 'authorization':
            conn.execute("UPDATE source_authorization SET revoked_at='gone'")
        elif change == 'selected_tag':
            conn.execute("UPDATE tag SET name='修改候选',name_norm='修改候选' WHERE name='已有乙'")
        elif change == 'key':
            access.delete_key('session')
        else:
            sessions.clear()
        return httpx.Response(200, json=envelope())
    behavior[0] = invalidate
    result = gateway.send(conn, 'session', p['preview_id'], confirmed=True)
    assert result['status'] == 'stale' and len(calls) == 1
    assert conn.execute('SELECT count(*) FROM classification_run').fetchone()[0] == 0
    assert conn.execute('SELECT count(*) FROM tag_assignment').fetchone()[0] == 0
    assert conn.execute('SELECT status FROM egress_log').fetchone()[0] == 'stale'


def test_budget_counts_unknown_failures_and_rolls_after_24_hours(gateway_data):
    conn, _, _, _, _, gateway, calls, behavior, now, _ = gateway_data
    def timeout(request):
        raise httpx.ReadTimeout('TEST-ONLY-GATEWAY-KEY PRIVATE', request=request)
    behavior[0] = timeout
    for _ in range(20):
        p = preview(gateway_data)
        result = gateway.send(conn, 'session', p['preview_id'], confirmed=True)
        assert result['status'] == 'outcome_unknown' and result['error_class'] == 'timeout'
        now[0] += 300.0  # 消费记录同样占预览名额，过期后清理；不推进24小时预算时钟。
    assert len(calls) == 20 and conn.execute('SELECT count(*) FROM egress_log').fetchone()[0] == 20
    with pytest.raises(ValueError, match=r'20|预算'):
        preview(gateway_data)
    assert len(calls) == 20
    conn.execute("UPDATE egress_log SET created_at='2000-01-01T00:00:00.000Z',dispatched_at='2000-01-01T00:00:00.000Z'")
    assert preview(gateway_data)['attempts_remaining'] == 20


def test_concurrent_attempt_is_rejected_without_queue_or_consumption(gateway_data):
    conn, _, _, _, _, gateway, calls, behavior, *_ = gateway_data
    first, second = preview(gateway_data), preview(gateway_data)
    def concurrent(_request):
        if len(calls) == 1:
            with pytest.raises(ValueError, match=r'并发|进行中'):
                gateway.send(conn, 'session', second['preview_id'], confirmed=True)
        return httpx.Response(200, json=envelope())
    behavior[0] = concurrent
    assert gateway.send(conn, 'session', first['preview_id'], confirmed=True)['status'] == 'succeeded'
    assert gateway.send(conn, 'session', second['preview_id'], confirmed=True)['status'] == 'succeeded'
    assert len(calls) == 2


def test_suggestion_write_failure_rolls_model_run_and_assignments_back(gateway_data):
    conn, _, _, _, _, gateway, calls, *_ = gateway_data
    p = preview(gateway_data)
    conn.execute("CREATE TRIGGER fail_run BEFORE INSERT ON classification_run BEGIN SELECT RAISE(ABORT,'PRIVATE FAILURE'); END")
    result = gateway.send(conn, 'session', p['preview_id'], confirmed=True)
    assert result['status'] == 'failed' and result['error_class'] == 'write_failure'
    for table in ['model_version', 'classification_run', 'tag_assignment']:
        assert conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0
    assert conn.execute('SELECT status FROM egress_log').fetchone()[0] == 'failed'
    assert len(calls) == 1
    gateway.send(conn, 'session', p['preview_id'], confirmed=True)
    assert len(calls) == 1


@pytest.mark.parametrize('change', ['body', 'catalog'])
def test_last_dispatch_check_rejects_changes_after_budget_reservation(gateway_data, change):
    conn, _, _, _, _, gateway, calls, *_ = gateway_data
    p = preview(gateway_data)
    sql = "UPDATE content_item SET text_content='changed'" if change == 'body' else "UPDATE tag SET name='changed',name_norm='changed' WHERE name='已有乙'"
    conn.execute(f"CREATE TRIGGER change_after_reservation AFTER INSERT ON egress_log BEGIN {sql}; END")
    result = gateway.send(conn, 'session', p['preview_id'], confirmed=True)
    assert result['status'] == 'stale' and calls == []
    assert conn.execute('SELECT dispatched_at FROM egress_log').fetchone()[0] is None
    assert gateway._remaining(conn) == 19  # 已提交预留保守计次，即使后续复查阻止出站。
    assert conn.execute('SELECT count(*) FROM classification_run').fetchone()[0] == 0
    assert gateway.send(conn, 'session', p['preview_id'], confirmed=True)['status'] == 'stale'


def test_factory_failure_has_terminal_sanitized_record_and_cannot_retry(gateway_data, caplog):
    conn, _, _, _, _, gateway, calls, *_ = gateway_data
    p = preview(gateway_data)
    def fail(_key):
        raise RuntimeError('TEST-ONLY-GATEWAY-KEY PRIVATE')
    gateway.provider_factory = fail
    result = gateway.send(conn, 'session', p['preview_id'], confirmed=True)
    assert result['status'] == 'failed' and result['error_class'] == 'provider_failure'
    assert calls == [] and 'PRIVATE' not in json.dumps(result)
    assert 'RuntimeError' in caplog.text and result['request_id'] in caplog.text
    assert 'TEST-ONLY-GATEWAY-KEY' not in caplog.text and 'PRIVATE' not in caplog.text
    assert gateway.send(conn, 'session', p['preview_id'], confirmed=True) == result


def test_preview_registry_limit_is_bounded_and_expired_entries_are_removed(gateway_data):
    _, _, _, _, access, _, _, _, now, _ = gateway_data
    for _ in range(8):
        preview(gateway_data)
    with pytest.raises(ValueError, match='预览'):
        preview(gateway_data)
    assert len(access.previews) == 8
    assert all('正文一' not in repr(value) for value in access.previews.values())
    now[0] = 300.0
    preview(gateway_data)
    assert len(access.previews) == 1
