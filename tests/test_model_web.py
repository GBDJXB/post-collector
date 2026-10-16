"""本机模型流程、闭合总闸、逐次同意、证明绑定与退出清理；仅模拟外网。"""
import json
from contextlib import nullcontext

import httpx
import pytest
from test_deepseek_model import envelope
from test_web_app import client as client
from test_web_app import login
from test_web_app import web as web

from pc.adapters import sqlite as db
from pc.adapters.deepseek_model import DeepSeekModel
from pc.services import compositions


@pytest.mark.asyncio
async def test_web_create_composition_preserves_order_and_independent_tags(client, web):
    await login(client, web)
    text = web['targets']['text']['content_id']
    image = web['targets']['image']['content_id']
    created = await client.post('/api/v1/compositions', json={'name': '网页组合', 'description': '有序', 'content_ids': [image, text, image]})
    assert created.status_code == 200, created.text
    ident = created.json()['composition_id']
    detail = (await client.get(f'/api/v1/compositions/{ident}')).json()
    assert [m['content_id'] for m in detail['members']] == [image, text, image]
    assert detail['description'] == '有序'
    added = await client.post(f'/api/v1/compositions/{ident}/tags/add', json={'names': ['仅组合']})
    assert added.status_code == 200, added.text
    assert [t['name'] for t in (await client.get(f'/api/v1/compositions/{ident}')).json()['tags']] == ['仅组合']
    tag = next(t for t in (await client.get('/api/v1/tags')).json()['tags'] if t['name'] == '仅组合')
    assert (tag['usage_count'], tag['composition_usage_count']) == (0, 1)
    assert (await client.post(f'/api/v1/compositions/{ident}/tags/remove', json={'names': ['仅组合']})).status_code == 200
    assert (await client.post(f'/api/v1/compositions/{ident}/tags/add', json={'names': ['仅组合']})).status_code == 200
    assert len((await client.get(f'/api/v1/compositions/{ident}/assignments')).json()['assignments']) == 1
    filtered = await client.get('/api/v1/compositions', params={'tags': '仅组合'})
    assert [row['composition_id'] for row in filtered.json()['compositions']] == [ident]
    assert (await client.get('/api/v1/compositions', params={'tags': '不存在'})).status_code == 400


@pytest.mark.asyncio
async def test_web_composition_directory_finds_created_by_name_and_id(client, web):
    assert (await client.get('/api/v1/compositions', params={'name': '网页'})).status_code == 401
    await login(client, web)
    content_id = web['targets']['text']['content_id']
    response = await client.post('/api/v1/compositions', json={'name': '午后_手记', 'content_ids': [content_id]})
    assert response.status_code == 200
    ident = response.json()['composition_id']
    other = await client.post('/api/v1/compositions', json={'name': '另一组', 'content_ids': [content_id]})
    assert other.status_code == 200
    assert (await client.get('/api/v1/compositions')).json()['total'] == 2
    for params in ({'name': '午后_'}, {'composition_id': ident}, {'composition_id': ident, 'name': '午后_'}):
        result = (await client.get('/api/v1/compositions', params=params)).json()
        assert result['total'] == 1
        assert result['compositions'][0]['composition_id'] == ident
        assert [(r['position'], r['content_id']) for r in result['compositions'][0]['members']] == [(0, content_id)]
    assert (await client.get('/api/v1/compositions', params={'composition_id': ident, 'name': '另一组'})).json()['total'] == 0
    assert (await client.get('/api/v1/compositions', params={'composition_id': 999999})).json()['total'] == 0
    assert (await client.get('/api/v1/compositions', params={'composition_id': 0})).status_code == 422
    assert (await client.get('/api/v1/compositions', params={'name': 'a' * 256})).status_code == 422


@pytest.mark.asyncio
async def test_web_composition_rejects_invalid_and_stale_members_atomically(client, web):
    await login(client, web)
    ident = web['targets']['text']['content_id']
    for body in [
        {'name': '空', 'content_ids': []},
        {'name': '坏', 'content_ids': [ident, True]},
        {'name': '坏', 'content_ids': [ident], 'actor_id': 1},
        {'name': '坏', 'content_ids': [ident, 999999]},
    ]:
        assert (await client.post('/api/v1/compositions', json=body)).status_code in {400, 422}
    with db.open_db(web['path']) as conn:
        conn.execute('UPDATE content_item SET deleted_at=? WHERE content_id=?', (db.now_iso(), ident))
    assert (await client.post('/api/v1/compositions', json={'name': '过期', 'content_ids': [web['targets']['image']['content_id'], ident]})).status_code == 400
    with db.open_db(web['path']) as conn:
        assert conn.execute('SELECT count(*) FROM composition').fetchone()[0] == 0


@pytest.mark.asyncio
async def test_web_composition_requires_session_origin_and_csrf(client, web):
    ident = web['targets']['text']['content_id']
    body = {'name': '保护', 'content_ids': [ident]}
    assert (await client.post('/api/v1/compositions', json=body)).status_code == 401
    await login(client, web)
    assert (await client.post('/api/v1/compositions', json=body, headers={'Origin': 'http://evil.example'})).status_code == 403
    assert (await client.post('/api/v1/compositions', json=body, headers={'X-CSRF-Token': 'invalid'})).status_code == 403
    with db.open_db(web['path']) as conn:
        assert conn.execute('SELECT count(*) FROM composition').fetchone()[0] == 0


@pytest.mark.asyncio
async def test_web_member_remove_is_position_specific_and_search_stops_matching(client, web):
    await login(client, web)
    text = web['targets']['text']['content_id']
    image = web['targets']['image']['content_id']
    created = await client.post('/api/v1/compositions', json={'name': '资料册', 'content_ids': [text, image, text]})
    ident = created.json()['composition_id']
    other = (await client.post('/api/v1/compositions', json={'name': '另一个', 'content_ids': [text]})).json()['composition_id']
    path = f'/api/v1/compositions/{ident}'
    stamp = (await client.get(path)).json()['updated_at']
    removed = await client.post(f'{path}/members/remove', json={'position': 0, 'expected_updated_at': stamp})
    assert removed.status_code == 200, removed.text
    detail = (await client.get(path)).json()
    assert [(m['position'], m['content_id']) for m in detail['members']] == [(0, image), (1, text)]
    removed = await client.post(f'{path}/members/remove', json={'position': 1, 'expected_updated_at': detail['updated_at']})
    assert removed.status_code == 200, removed.text
    detail = (await client.get(path)).json()
    assert [m['content_id'] for m in detail['members']] == [image]
    found = (await client.get('/api/v1/search', params={'result_version': 2, 'q': '网页测试中文'})).json()['hits']
    assert ident not in [r['composition_id'] for r in found if r['kind'] == 'composition']
    assert other in [r['composition_id'] for r in found if r['kind'] == 'composition']
    assert text in [r['content_id'] for r in found if r['kind'] == 'content']
    with db.open_db(web['path']) as conn:
        assert conn.execute('SELECT count(*) FROM content_item').fetchone()[0] > 0


@pytest.mark.asyncio
async def test_web_member_force_empty_stale_edit_and_soft_delete(client, web):
    await login(client, web)
    text = web['targets']['text']['content_id']
    image = web['targets']['image']['content_id']
    ident = (await client.post('/api/v1/compositions', json={'name': '待编辑', 'content_ids': [text]})).json()['composition_id']
    path = f'/api/v1/compositions/{ident}'
    first = (await client.get(path)).json()['updated_at']
    reject = await client.post(f'{path}/members/remove', json={'position': 0, 'expected_updated_at': first})
    assert reject.status_code == 400
    assert (await client.get(path)).json()['members'][0]['content_id'] == text
    allowed = await client.post(f'{path}/members/remove', json={'position': 0, 'force_empty': True, 'expected_updated_at': first})
    assert allowed.status_code == 200, allowed.text
    empty = (await client.get(path)).json()
    assert empty['members'] == [] and empty['updated_at'] != first
    stale = await client.post(f'{path}/members/add', json={'content_id': image, 'expected_updated_at': first})
    assert stale.status_code == 400
    assert (await client.get(path)).json()['members'] == []
    added = await client.post(f'{path}/members/add', json={'content_id': image, 'expected_updated_at': empty['updated_at']})
    assert added.status_code == 200, added.text
    detail = (await client.get(path)).json()
    appended = await client.post(f'{path}/members/add', json={'content_id': text, 'expected_updated_at': detail['updated_at']})
    assert appended.status_code == 200, appended.text
    detail = (await client.get(path)).json()
    moved = await client.post(f'{path}/members/move', json={'position': 1, 'to_position': 0, 'expected_updated_at': detail['updated_at']})
    assert moved.status_code == 200, moved.text
    detail = (await client.get(path)).json()
    assert [m['content_id'] for m in detail['members']] == [text, image]
    updated = await client.post(f'{path}/update', json={'name': '已编辑', 'description': '新说明', 'expected_updated_at': detail['updated_at']})
    assert updated.status_code == 200, updated.text
    detail = (await client.get(path)).json()
    assert (detail['name'], detail['description']) == ('已编辑', '新说明')
    deleted = await client.post(f'{path}/delete', json={'expected_updated_at': detail['updated_at']})
    assert deleted.status_code == 200, deleted.text
    assert (await client.get(path)).status_code == 400
    with db.open_db(web['path']) as conn:
        assert conn.execute('SELECT count(*) FROM content_item WHERE content_id=?', (text,)).fetchone()[0] == 1
        assert conn.execute('SELECT count(*) FROM composition_member WHERE composition_id=?', (ident,)).fetchone()[0] == 2


@pytest.mark.asyncio
async def test_web_member_mutations_reject_invalid_inputs_and_unauthorized(client, web):
    text = web['targets']['text']['content_id']
    missing = await client.post('/api/v1/compositions/1/members/remove', json={'position': 0, 'expected_updated_at': 'x'})
    assert missing.status_code == 401
    await login(client, web)
    ident = (await client.post('/api/v1/compositions', json={'name': '受保护', 'content_ids': [text]})).json()['composition_id']
    path = f'/api/v1/compositions/{ident}'
    stamp = (await client.get(path)).json()['updated_at']
    body = {'position': 0, 'expected_updated_at': stamp}
    assert (await client.post(f'{path}/members/remove', json=body, headers={'Origin': 'http://evil.example'})).status_code == 403
    assert (await client.post(f'{path}/members/remove', json=body, headers={'Host': 'evil.example'})).status_code == 403
    assert (await client.post(f'{path}/members/remove', json=body, headers={'X-CSRF-Token': 'invalid'})).status_code == 403
    for invalid in ({**body, 'position': True}, {**body, 'position': -1}, {**body, 'actor_id': 1}, {'position': 0}):
        assert (await client.post(f'{path}/members/remove', json=invalid)).status_code == 422
    assert (await client.post(f'{path}/members/add', json={'content_id': 999999, 'expected_updated_at': stamp})).status_code == 400
    assert (await client.get(path)).json()['members'][0]['content_id'] == text
    with db.open_db(web['path']) as conn:
        compositions.write(conn, 'set-members', ident=ident, contents=[text] * 500)
    stamp = (await client.get(path)).json()['updated_at']
    over = await client.post(f'{path}/members/add', json={'content_id': text, 'expected_updated_at': stamp})
    assert over.status_code == 400
    assert len((await client.get(path)).json()['members']) == 500


async def setup_model(client, web):
    await login(client, web)
    response = await client.post('/api/v1/tags/add', json={'content_ids': [web['targets']['text']['content_id']], 'names': ['已有']})
    assert response.status_code == 200
    response = await client.post('/api/v1/model/key/set', json={'key': 'TEST-ONLY-WEB-KEY'})
    assert response.status_code == 200, response.text
    assert response.json()['key_present'] and not response.json()['enabled']
    response = await client.post('/api/v1/model/access', json={'enabled': True, 'confirmed': True})
    assert response.status_code == 200, response.text
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=envelope())
    web['app'].state.model_gateway.provider_factory = lambda key: DeepSeekModel(key, _transport=httpx.MockTransport(handle))
    return calls


@pytest.mark.asyncio
async def test_model_routes_are_protected_and_default_closed(client, web):
    assert (await client.get('/api/v1/model/status')).status_code == 401
    assert (await client.post('/api/v1/model/key/set', json={'key': 'SECRET'})).status_code == 401
    await login(client, web)
    status = (await client.get('/api/v1/model/status')).json()
    assert status['key_present'] is False and status['enabled'] is False
    assert (await client.post('/api/v1/model/preview', json={'content_id': web['targets']['text']['content_id']})).status_code == 400


@pytest.mark.asyncio
async def test_meta_reports_only_current_valid_session_model_access(client, web):
    assert (await client.get('/api/v1/meta')).json()['egress_enabled'] is False
    await setup_model(client, web)
    assert (await client.get('/api/v1/meta')).json()['egress_enabled'] is True
    assert (await client.post('/api/v1/logout', json={})).status_code == 200
    assert (await client.get('/api/v1/meta')).json()['egress_enabled'] is False


@pytest.mark.asyncio
async def test_key_validation_errors_never_echo_credentials(client, web):
    await login(client, web)
    for body in [{'key': 'TEST-ONLY-SECRET', 'actor_id': 1}, {'key': ['TEST-ONLY-SECRET']}, {'key': 'TEST-ONLY-SECRET\n'}]:
        response = await client.post('/api/v1/model/key/set', json=body)
        assert response.status_code in {400, 422}
        assert 'TEST-ONLY-SECRET' not in response.text


@pytest.mark.asyncio
async def test_content_preview_send_then_client_bound_confirmation(client, web):
    calls = await setup_model(client, web)
    ident = web['targets']['text']['content_id']
    # 撤销用于播种目录的人工关联，使模型建议的非生效状态可观察。
    assert (await client.post('/api/v1/tags/remove', json={'content_ids': [ident], 'names': ['已有']})).status_code == 200
    p = await client.post('/api/v1/model/preview', json={'content_id': ident})
    assert p.status_code == 200, p.text
    assert calls == [] and p.json()['parts'][0]['text']
    assert (await client.post('/api/v1/model/send', json={'preview_id': p.json()['preview_id'], 'confirmed': False})).status_code == 400
    result = await client.post('/api/v1/model/send', json={'preview_id': p.json()['preview_id'], 'confirmed': True})
    assert result.status_code == 200 and result.json()['status'] == 'succeeded'
    assert len(calls) == 1
    assignments = (await client.get(f'/api/v1/assignments?content_ids={ident}')).json()['assignments']
    entry = next(x for x in assignments if x['source'] == 'suggested')
    proof = entry['evidence']['model_proposal']
    assert (await client.post('/api/v1/suggestions/confirm', json={'ids': [entry['assign_id']]})).status_code == 400
    response = await client.post('/api/v1/suggestions/confirm', json={'ids': [entry['assign_id']], 'expected_runs': {str(entry['assign_id']): proof}})
    assert response.status_code == 200, response.text
    with db.open_db(web['path']) as conn:
        assert conn.execute("SELECT count(*) FROM tag_assignment WHERE source='human' AND revoked_at IS NULL").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM tag_assignment WHERE source='suggested' AND revoked_at IS NULL").fetchone()[0] == 0
        assert 'TEST-ONLY-WEB-KEY' not in json.dumps([dict(r) for r in conn.execute('SELECT * FROM egress_log')])


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['logout', 'idle', 'delete', 'replace'])
async def test_session_key_lifecycle_revokes_arm_and_previews(client, web, reason):
    calls = await setup_model(client, web)
    await client.post('/api/v1/model/preview', json={'content_id': web['targets']['text']['content_id']})
    access = web['app'].state.model_access
    if reason == 'logout':
        assert (await client.post('/api/v1/logout', json={})).status_code == 200
    elif reason == 'idle':
        next(iter(web['app'].state.sessions.entries.values()))['seen'] -= 1801
        assert (await client.get('/api/v1/session')).status_code == 401
    elif reason == 'delete':
        assert (await client.post('/api/v1/model/key/delete', json={})).status_code == 200
    else:
        response = await client.post('/api/v1/model/key/set', json={'key': 'REPLACEMENT-TEST-KEY'})
        assert response.json()['enabled'] is False
    assert not access.previews and access._armed is False and calls == []
    if reason != 'replace':
        assert access._key is None


@pytest.mark.asyncio
@pytest.mark.parametrize('interrupted', [False, True], ids=['normal', 'exception'])
async def test_app_shutdown_clears_key_and_previews_without_delete(client, web, interrupted):
    app = web['app']
    access = app.state.model_access
    expected_exit = pytest.raises(RuntimeError, match='测试生命周期中断') if interrupted else nullcontext()
    with expected_exit:
        async with app.router.lifespan_context(app):
            await setup_model(client, web)
            response = await client.post('/api/v1/model/preview', json={'content_id': web['targets']['text']['content_id']})
            assert response.status_code == 200, response.text
            assert access.previews and access._armed is True
            generation = access.generation
            if interrupted:
                raise RuntimeError('测试生命周期中断')

    # 保留 app/access 引用；断言应用关闭真正清除了凭据，而非依赖 GC。
    assert access._key is None and access._owner is None
    assert access._armed is False and access.previews == {}
    assert access.generation > generation
    status = (await client.get('/api/v1/model/status')).json()
    assert status['key_present'] is False and status['enabled'] is False
    with db.open_db(web['path']) as conn:
        assert db.read_settings(conn)['privacy.local_only_mode'].value is False
    refused = await client.post('/api/v1/model/preview', json={'content_id': web['targets']['text']['content_id']})
    assert refused.status_code == 400 and '未放行' in refused.text


@pytest.mark.asyncio
async def test_composition_proposal_is_ordered_and_never_propagates(client, web):
    calls = await setup_model(client, web)
    ident = web['targets']['text']['content_id']
    with db.open_db(web['path']) as conn:
        composition = compositions.write(conn, 'create', name='独立组合', contents=[ident, ident])['composition_id']
    shown = await client.get(f'/api/v1/compositions/{composition}')
    assert shown.status_code == 200 and [x['content_id'] for x in shown.json()['members']] == [ident, ident]
    assert (await client.get('/api/v1/compositions')).json()['total'] == 1
    p = await client.post('/api/v1/model/preview', json={'composition_id': composition})
    assert p.status_code == 200 and [x['position'] for x in p.json()['parts']] == [0, 1]
    result = await client.post('/api/v1/model/send', json={'preview_id': p.json()['preview_id'], 'confirmed': True})
    assert result.json()['status'] == 'succeeded' and len(calls) == 1
    entry = (await client.get(f'/api/v1/compositions/{composition}/assignments')).json()['assignments'][0]
    proof = json.loads(entry['evidence'])['model_proposal']
    response = await client.post(f'/api/v1/compositions/{composition}/suggestions/confirm', json={'ids': [entry['assign_id']], 'expected_runs': {str(entry['assign_id']): proof}})
    assert response.status_code == 200, response.text
    assert [x['name'] for x in (await client.get(f'/api/v1/compositions/{composition}')).json()['tags']] == ['已有']
    with db.open_db(web['path']) as conn:
        assert conn.execute("SELECT count(*) FROM tag_assignment WHERE source='suggested'").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_model_write_boundaries_keep_host_origin_csrf_and_target_xor(client, web):
    await setup_model(client, web)
    body = {'content_id': web['targets']['text']['content_id']}
    for headers in [{'Host': 'evil.example'}, {'Origin': 'http://evil.example'}, {'X-CSRF-Token': 'wrong'}]:
        assert (await client.post('/api/v1/model/preview', json=body, headers=headers)).status_code == 403
    assert (await client.post('/api/v1/model/preview', json={**body, 'composition_id': 1})).status_code == 400
    assert (await client.post('/api/v1/model/preview', json={**body, 'actor_id': 1})).status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['confirm', 'reject'])
async def test_model_review_cannot_cross_composition_target(client, web, operation):
    calls = await setup_model(client, web)
    ident = web['targets']['text']['content_id']
    with db.open_db(web['path']) as conn:
        first = compositions.write(conn, 'create', name='建议所属组合', contents=[ident])['composition_id']
        second = compositions.write(conn, 'create', name='另一个组合', contents=[ident])['composition_id']
    p = (await client.post('/api/v1/model/preview', json={'composition_id': first})).json()
    result = await client.post('/api/v1/model/send', json={'preview_id': p['preview_id'], 'confirmed': True})
    assert result.json()['status'] == 'succeeded' and len(calls) == 1
    original = (await client.get(f'/api/v1/compositions/{first}/assignments')).json()
    entry = original['assignments'][0]
    proof = json.loads(entry['evidence'])['model_proposal']
    response = await client.post(f'/api/v1/compositions/{second}/suggestions/{operation}', json={
        'ids': [entry['assign_id']], 'expected_runs': {str(entry['assign_id']): proof},
    })
    assert response.status_code == 400, response.text
    assert (await client.get(f'/api/v1/compositions/{first}/assignments')).json() == original
    assert (await client.get(f'/api/v1/compositions/{second}/assignments')).json()['assignments'] == []
    assert len(calls) == 1
