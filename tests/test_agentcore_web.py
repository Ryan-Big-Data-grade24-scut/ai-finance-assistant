"""走应用的真实启动、HTTP 路由和账本事务；不监听端口，不接外部模型。"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager, closing
from datetime import timedelta
from threading import Event

import httpx
import pytest

from agentcore_evidence import Evidence
from agentcore_support import (DEMO_CSV, DEMO_FILENAME, FOLLOW_UP, GROUP_NAME, IMPORT_GOAL, NODE_NAME,
                               call_reply, demo_model, install_config, seed_demo_history, tool)
from backend import db
from backend.agentcore import adapter, import_tool, registry, runtime, session

AC = '/api/agentcore'
TREE = '/api/ledger2/tree'
QUERY = 'finance_query_transactions'
APPLY = 'finance_ledger_apply'
COMMIT = 'finance_import_commit'


def rows(sql, params=()):
    with closing(db.get_conn()) as conn:
        return [tuple(row) for row in conn.execute(sql, params)]


@pytest.fixture
def app(agentcore_paths, monkeypatch):
    from backend.main import app

    install_config(monkeypatch)
    monkeypatch.setattr(runtime, 'POLL_SECONDS', 0.02)
    return app


@asynccontextmanager
async def web(app):
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test.invalid') as client:
            yield client


async def body(request, status=200):
    response = await asyncio.wait_for(request, 10)
    assert response.status_code == status, response.text
    return response.json()


async def create(client, title):
    info = await body(client.post(f'{AC}/sessions', json={'title': title}))
    return f"{AC}/sessions/{info['id']}"


def frames_from(text):
    frames = []
    for block in text.split('\n\n'):
        if not block or block.startswith(':'):
            continue
        id_line, data = block.splitlines()
        frame = json.loads(data.removeprefix('data: '))
        assert id_line == f"id: {frame['cursor']}"
        frames.append(frame)
    return frames


async def events(client, path, *, after=0, state='completed', code=None):
    response = await asyncio.wait_for(client.get(f'{path}/events', params={'after': after}), 15)
    assert response.status_code == 200 and response.headers['content-type'].startswith('text/event-stream')
    frames = frames_from(response.text)
    assert [f['cursor'] for f in frames] == sorted({f['cursor'] for f in frames})
    last = frames[-1]
    assert last['type'] == 'terminal' and last['item']['run_id'] == path.rsplit('/', 1)[1], frames
    assert (last['item']['state'], last['item']['code']) == (state, code), last
    return frames


def results(frames):
    return {f['item']['call_id']: f['item'] for f in frames if f.get('item', {}).get('type') == 'tool'}


def check_detail(detail, run, frames):
    last = frames[-1]
    assert detail['cursor'] == detail['run']['end_id'] == last['cursor']
    assert detail['run']['id'] == run['id'] and detail['run']['state'] == last['item']['state']
    assert [m for m in detail['messages'] if m['id'] >= run['start_id']] == [f['item'] for f in frames if 'item' in f]


def test_session_list_tracks_renames_active_runs_and_deletions(app, monkeypatch):
    async def main():
        model_release = asyncio.Event()

        async def model(_config, **_request):
            await model_release.wait()
            return adapter.ModelReply(text='会话运行完成。')

        monkeypatch.setattr(adapter, 'complete', model)
        async with web(app) as client:
            first = await create(client, '第一个会话')
            second = await create(client, '第二个会话')
            sid = first.rsplit('/', 1)[1]
            other = second.rsplit('/', 1)[1]

            async def catalog():
                reply = await body(client.get(f'{AC}/sessions'))
                return {row['id']: row for row in reply['sessions']}

            assert set(await catalog()) == {sid, other}
            invalid = await client.patch(first, json={'title': '   '})
            assert invalid.status_code in (400, 422)
            run = await body(client.post(f'{first}/turns', json={'text': '查看账本'}), 202)
            try:
                renamed = await body(client.patch(first, json={'title': '  已改名  '}))
                assert renamed['id'] == sid and renamed['title'] == '已改名'
                rows = await catalog()
                assert rows[sid]['title'] == '已改名' and rows[sid]['active_run_id'] == run['id']
                assert rows[other]['active_run_id'] is None
                rejected = await body(client.delete(first), 409)
                assert rejected['code'] == 'RUN_ACTIVE'
                assert (await body(client.get(first)))['session']['title'] == '已改名'
            finally:
                model_release.set()
            await events(client, f"{first}/turns/{run['id']}")
            assert (await catalog())[sid]['active_run_id'] is None
            await body(client.delete(first))
            assert set(await catalog()) == {other}
            assert (await body(client.patch(first, json={'title': '删除后修改'}), 404))['code'] == 'SESSION_NOT_FOUND'

    asyncio.run(main())


def test_startup_upgrades_view_and_two_turns_use_actual_receipts(app, agentcore_paths, agentcore_evidence, monkeypatch):
    old_view = 'CREATE VIEW agentcore_tx AS SELECT id AS tx_id, amount AS old_amount FROM transactions_raw'
    db.init_db()
    prior = seed_demo_history(agentcore_paths)
    with closing(db.get_conn()) as conn, conn:
        conn.execute(old_view)
    assert rows('SELECT old_amount FROM agentcore_tx ORDER BY tx_id') == [(8.0,), (9.0,)]
    calls = []
    after_commit = {}

    async def model(config, **request):
        reply = await demo_model(config, **request)
        calls.extend(reply.tool_calls)
        if reply.tool_calls and reply.tool_calls[0].id == 'candidates':
            after_commit['raw'] = rows('SELECT * FROM transactions_raw ORDER BY id')
            assert rows('SELECT * FROM ledger2_tx') == []  # commit 本身不初始化
            evidence.checkpoint('入库尚未初始化')
        return reply

    monkeypatch.setattr(adapter, 'complete', model)

    async def main():
        nonlocal evidence
        async with web(app) as client:
            assert rows('SELECT amount_cents, initialized FROM agentcore_tx ORDER BY tx_id') == [(800, 0), (900, 0)]
            evidence = Evidence(agentcore_evidence, agentcore_paths, 'A_导入_建组_刷新后解散')
            evidence.step('启动应用：原型旧视图 `agentcore_tx` → 当前视图', '验证在真实 lifespan 内替换，而非测试夹具代建。',
                          '旧视图读到 8.0/9.0 元；启动后是 800/900 分，两笔均未初始化。CSV 初始快照从 startup 完成后开始。')
            baseline = rows('SELECT * FROM transactions_raw ORDER BY id')
            initial_batches = rows('SELECT batch_id,inserted FROM import_batches')
            initial = await body(client.get(TREE))
            assert initial['uninit'] == 2 and initial['tree']['count'] == 0
            url = await create(client, '合成两轮场景')
            sid = url.rsplit('/', 1)[1]
            stage = await body(client.post(f'{AC}/upload', files={'file': (DEMO_FILENAME, DEMO_CSV, 'text/csv')}))
            stage_id = stage['stage_id']
            stage_url = f'{AC}/import/stages/{stage_id}'
            assert (stage['state'], stage['total'], stage['failed']) == ('staged', 3, 0)
            restored = await body(client.get(stage_url))
            assert all(restored[k] == v for k, v in stage.items())
            old_card = await body(client.post(f'{AC}/import/prepare', json={'stage_id': stage_id, 'session_id': sid}))
            assert (old_card['state'], old_card['preview']['duplicate_estimate']) == ('pending', 1)
            assert rows('SELECT * FROM transactions_raw ORDER BY id') == baseline
            assert rows('SELECT batch_id,inserted FROM import_batches') == initial_batches
            assert await body(client.get(TREE)) == initial
            evidence.step('POST `/upload`；GET `/import/stages/{stage_id}`；生成旧式手工卡（不确认）',
                          '确认解析、入库、V2 初始化是三件事；并验证有旧待确认卡时仍能执行新回合。',
                          f'解析 3 笔，预估 1 笔重复；原始表、批次与 V2 树未变。暂存 `{stage_id}`，旧卡 `{old_card["action_id"]}`。')
            evidence.checkpoint('已解析未入库')

            first = await body(client.post(f'{url}/turns', json={'text': IMPORT_GOAL, 'page': {
                'route': '/v2/assistant', 'staged_import': {'stage_id': stage_id, 'total': stage['total']},
            }}), 202)
            first_path = f"{url}/turns/{first['id']}"
            first_frames = await events(client, first_path)
            tool_items = results(first_frames)
            actual = {k: v['result'] for k, v in tool_items.items()}
            assert len(tool_items) == 15 and all(not v['is_error'] for v in tool_items.values())
            assert actual['stage']['stage']['stage_id'] == stage_id
            receipt = actual['commit']['receipt']
            batch_id = receipt['batch_id']
            assert (receipt['inserted'], receipt['skipped'], receipt['failed']) == (2, 1, 0)
            assert await body(client.get(stage_url)) == {k: v for k, v in actual['commit'].items() if k != 'ok'}
            ids = [r[0] for r in rows('SELECT id FROM transactions_raw WHERE import_batch=? ORDER BY id', (batch_id,))]
            assert len(ids) == 2 and batch_id != prior
            assert actual['candidates']['rows'] == [[ids[0], '餐饮', '支出', 3200], [ids[1], '其他收入', '收入', 1200]]
            assert len(after_commit['raw']) == 4 and after_commit['raw'][:2] == baseline
            assert rows('SELECT * FROM transactions_raw ORDER BY id') == after_commit['raw']
            assert {c.id for c in calls if c.name == 'read_skill'} == {'skill-import', 'skill-init', 'skill-classify', 'skill-select'}
            assert actual['examples']['examples'] == []
            node_id = actual['init']['created']['nodes']['new']
            group_id = actual['group']['created']['group_id']
            refs = {f'tx:{i}' for i in ids}
            grouped = await body(client.get(TREE))
            node, = grouped['tree']['children']
            group, = node['children']
            assert (node['id'], node['name'], group['id'], group['name']) == (node_id, NODE_NAME, group_id, GROUP_NAME)
            assert {x['ref'] for x in group['members']} == refs
            assert (group['e'], group['i'], group['count']) == (2000, 0, 2)
            assert (grouped['tree']['e'], grouped['tree']['i'], grouped['uninit']) == (2000, 0, 2)
            assert (actual['group-read']['node']['e'], actual['group-read']['node']['i']) == (2000, 0)
            assert actual['root']['root']['e'] == grouped['tree']['e'] and not grouped['status']['warnings']
            assert rows('SELECT tx_id,node_id,net_group_id FROM ledger2_tx ORDER BY tx_id') == [(i, node_id, group_id) for i in ids]
            assert [r[0] for r in rows('SELECT id FROM transactions_raw WHERE import_batch=? ORDER BY id', (prior,))] == [r[0] for r in baseline]
            before_refresh = await body(client.get(url))
            check_detail(before_refresh, first, first_frames)
            assert before_refresh['step'] == 16 and len(before_refresh['todos']) == 3
            assert all(t['status'] == 'completed' for t in before_refresh['todos'])
            assert [(a['id'], a['state']) for a in before_refresh['actions']] == [(old_card['action_id'], 'pending')]
            evidence.step(f'POST `/turns`（第一轮）\n\n> {IMPORT_GOAL}',
                          '让脚本从暂存回执取批次、再用实际 V2 节点／组引用继续；同时检查独立 V2 API。',
                          f'15 次工具调用，16 步；新增 2、跳过 1、失败 0，批次 `{batch_id}`。候选 `{ids}`；入库后 V2 仍空。'
                          f'节点 `{node_id}` 下组 `{group_id}` 只有这两笔，组 `e=2000, i=0`；原先两笔保持未初始化。旧卡仍待确认。')
            evidence.checkpoint('已建净额组')

            # 新 HTTP 客户端模拟刷新；后续请求不带任何对象/附件 ID。
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test.invalid') as refreshed:
                assert await body(refreshed.get(url)) == before_refresh
                second = await body(refreshed.post(f'{url}/turns', json={'text': FOLLOW_UP}), 202)
                second_frames = await events(refreshed, f"{url}/turns/{second['id']}")
                detail = await body(refreshed.get(url))
                check_detail(detail, second, second_frames)
                final_tree = await body(refreshed.get(TREE))
            assert second['id'] != first['id']
            follow = results(second_frames)
            assert list(follow) == ['dissolve', 'after'] and all(not v['is_error'] for v in follow.values())
            follow_calls = {c.id: json.loads(c.arguments) for c in calls if c.id in follow}
            assert follow_calls == {'dissolve': {'op': 'dissolve_group', 'group_id': group_id},
                                    'after': {'mode': 'node', 'ref': node_id}}
            final_node, = final_tree['tree']['children']
            assert final_node['id'] == node_id and {x['ref'] for x in final_node['children']} == refs
            assert (final_node['e'], final_node['i'], final_tree['uninit']) == (3200, 1200, 2)
            assert final_node['e'] - final_node['i'] == group['e'] - group['i']
            read_node = follow['after']['result']['node']
            assert (read_node['e'], read_node['i']) == (final_node['e'], final_node['i'])
            assert rows('SELECT * FROM transactions_raw ORDER BY id') == after_commit['raw']
            assert rows('SELECT * FROM ledger2_net_group') == []
            assert rows('SELECT tx_id,node_id,net_group_id FROM ledger2_tx ORDER BY tx_id') == [(i, node_id, None) for i in ids]
            assert rows('SELECT COUNT(*) FROM ledger2_op_log') == rows('SELECT COUNT(*) FROM agentcore_ledger_receipt') == [(3,)]
            assert rows('SELECT COUNT(*) FROM import_batches') == [(2,)]
            assert [m['text'] for m in detail['messages'] if m['type'] == 'user'] == [IMPORT_GOAL, FOLLOW_UP]
            assert [(a['id'], a['state']) for a in detail['actions']] == [(old_card['action_id'], 'pending')]
            evidence.step(f'新客户端 GET 会话，再 POST `/turns`（第二轮）\n\n> {FOLLOW_UP}',
                          '客户端不补对象参数，必须从上一轮真实回执找到要解散的组。',
                          '解散实际成功；同一节点下恢复两笔直挂，`e=3200, i=1200`，净支出仍为 ¥20。'
                          '原始流水与入库后逐字段一致；合计三次 V2 写入、三条写回执，旧卡未自动处理。')
            evidence.finish(final_tree, [(sid, '会话'), (first['id'], '导入并建组'), (second['id'], '续接解散'),
                                          (stage_id, '上传暂存'), (prior, '原有两笔的批次'), (batch_id, '本次导入批次'),
                                          (old_card['action_id'], '旧手工卡'), (node_id, NODE_NAME), (group_id, GROUP_NAME),
                                          (ids[0], '新增支出 32 元'), (ids[1], '新增收入 12 元')], artifacts={
                '第一轮_SSE.json': first_frames, '第二轮_SSE.json': second_frames,
                '第一轮_读出树.json': grouped, '刷新读会话.json': before_refresh, '终态会话.json': detail,
            })

    evidence = None
    asyncio.run(main())


async def disconnect_after_user(app, path):
    """直接跑 ASGI 的 receive/send，真的在未终止的响应中送 http.disconnect。"""
    incoming = asyncio.Queue()
    incoming.put_nowait({'type': 'http.request', 'body': b'', 'more_body': False})
    chunks, status = [], []
    saw_user = asyncio.Event()

    async def send(message):
        if message['type'] == 'http.response.start':
            status.append(message['status'])
        else:
            chunks.append(message.get('body', b'').decode())
            if any(f.get('item', {}).get('type') == 'user' for f in frames_from(chunks[-1])):
                saw_user.set()
                await asyncio.Future()  # 由断连取消，而不是等待 worker 结束

    scope = {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.3'}, 'http_version': '1.1',
             'method': 'GET', 'scheme': 'http', 'path': path, 'query_string': b'after=0', 'headers': [],
             'client': ('127.0.0.1', 12345), 'server': ('test.invalid', 80)}
    task = asyncio.create_task(app(scope, incoming.get, send))
    try:
        await asyncio.wait_for(saw_user.wait(), 5)
        assert not task.done()
        incoming.put_nowait({'type': 'http.disconnect'})
        await asyncio.wait_for(task, 5)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert status == [200]
    return frames_from(''.join(chunks))


def test_one_active_run_disconnect_resume_and_terminal_retrieval(app, agentcore_paths, agentcore_evidence, monkeypatch):
    async def main():
        release = asyncio.Event()
        called = []

        async def model(_cfg, **request):
            called.append(request['messages'][-1]['content'])
            await release.wait()
            return adapter.ModelReply(text='断开后仍完成')

        monkeypatch.setattr(adapter, 'complete', model)
        async with web(app) as client:
            evidence = Evidence(agentcore_evidence, agentcore_paths, 'B_并发启动_断线与游标')
            url = await create(client, '合成断线场景')
            try:
                contenders = await asyncio.wait_for(asyncio.gather(*[
                    client.post(f'{url}/turns', json={'text': text}) for text in ('并发请求甲', '并发请求乙')
                ]), 10)
                accepted, rejected = sorted(contenders, key=lambda r: r.status_code)
                assert accepted.status_code == 202 and rejected.status_code == 409 and rejected.json()['code'] == 'RUN_ACTIVE'
                winner = ('并发请求甲', '并发请求乙')[contenders.index(accepted)]
                run = accepted.json()
                path = f"{url}/turns/{run['id']}"
                prefix = await disconnect_after_user(app, f'{path}/events')
                assert prefix[-1]['item']['text'] == winner
                cursor = prefix[-1]['cursor']
                active = await body(client.get(url))
                assert active['run']['id'] == run['id'] and active['run']['state'] == 'running' and active['run']['end_id'] is None
                evidence.step('同一会话并行 POST 两次；订阅看到用户消息后发送 ASGI `http.disconnect`',
                              '用真实断开信号，不把缓冲整个响应的 transport 当成断开。',
                              f'一个 202、一个 `409 RUN_ACTIVE`；游标 {cursor} 断开后原轮仍运行。')
                release.set()
                suffix = await events(client, path, after=cursor)
                assert all(f['cursor'] > cursor for f in suffix)
                assert called == [winner]
                detail = await body(client.get(url))
                check_detail(detail, run, prefix + suffix)
                assert await events(client, path, after=detail['cursor']) == [suffix[-1]]
                assert rows('SELECT COUNT(*) FROM agentcore_run') == [(1,)]
                evidence.step('释放模型；从最后游标重订，再以终态游标订阅', '验证断开不停止也不重启运行，回放不漏帧不重复。',
                              f'模型只执行 1 次，后续帧游标均大于 {cursor}；终态游标 {detail["cursor"]} 可再次查得同一终态。')
                evidence.finish(await body(client.get(TREE)), [(run['id'], '唯一运行')], artifacts={
                    '断开前_SSE.json': prefix, '续接_SSE.json': suffix, '终态会话.json': detail,
                })
            finally:
                release.set()

    asyncio.run(main())


def test_selected_skill_loads_before_task_and_stays_out_of_user_text(app, agentcore_paths, agentcore_evidence, monkeypatch):
    requests = []

    async def model(_cfg, **request):
        requests.append(request)
        return adapter.ModelReply(text='合成验证完成')

    monkeypatch.setattr(adapter, 'complete', model)

    async def main():
        async with web(app) as client:
            evidence = Evidence(agentcore_evidence, agentcore_paths, '技能_显式加载与普通对话')
            catalog = (await body(client.get(f'{AC}/skills')))['skills']
            skill_id = 'ledger-classify'
            skill, = (s for s in catalog if s['id'] == skill_id)
            assert skill['label'] == '自动分类与参考历史'
            assert all(set(s) == {'id', 'label', 'description', 'when_to_use'} for s in catalog)
            text = '示例：按现有分类习惯给我建议，不修改账本。\n分两点回答。'
            url = await create(client, '显式技能合成验证')
            rejected = await body(client.post(f'{url}/turns', json={'text': text, 'skill_id': '../missing'}), 404)
            assert rejected['code'] == 'SKILL_NOT_FOUND' and not requests
            assert rows('SELECT * FROM agentcore_run') == []
            evidence.step('GET /skills；POST 不存在的 skill_id', '只提供可发现的摘要，过期 ID 不开启运行。',
                          '目录没有正文和路径；404 SKILL_NOT_FOUND；没有运行或模型请求。')

            run = await body(client.post(f'{url}/turns', json={'text': text, 'skill_id': skill_id}), 202)
            frames = await events(client, f"{url}/turns/{run['id']}")
            detail = await body(client.get(url))
            check_detail(detail, run, frames)
            user, = (m for m in detail['messages'] if m['type'] == 'user')
            assert (user['text'], user['skill']) == (text, {'id': skill_id, 'label': skill['label']})
            assert results(frames) == {}
            first, = requests
            assert first['messages'][-1] == {'role': 'user', 'content': text}
            content = first['messages'][-2]['content']
            assert '<turn_context>' in content and f'<skill_content name="{skill_id}">' in content
            assert '## 操作顺序' in content and '## 操作顺序' not in first['system']
            evidence.step('带独立 skill_id 发送；订阅 SSE 并重读会话', '区分用户正文、技能正文与历史标签。',
                          '首个任务请求已有技能正文，用户正文原样；SSE 与历史都保留技能标签，无伪造工具事件。')

            plain_text = '下一轮：这条消息不选技能。'
            plain = await body(client.post(f'{url}/turns', json={'text': plain_text}), 202)
            tail = await events(client, f"{url}/turns/{plain['id']}")
            next_user, = (f['item'] for f in tail if f.get('item', {}).get('type') == 'user')
            assert next_user['text'] == plain_text and 'skill' not in next_user
            assert len(requests) == 2
            second = requests[1]
            assert second['system'] == first['system'] and second['tools'] == first['tools']
            assert second['messages'][-1] == {'role': 'user', 'content': plain_text}
            assert all('<skill_content' not in m['content'] for m in second['messages'])
            assert rows('SELECT * FROM transactions_raw') == [] and rows('SELECT * FROM ledger2_tx') == []
            evidence.step('同一会话不选技能再发送', '技能仅作用本轮，不影响稳定前缀或账本。',
                          '无标签与技能正文，系统文本和工具不变，原始流水与 V2 摆放未写入。')
            evidence.finish(await body(client.get(TREE)), [(run['id'], '显式选择'), (plain['id'], '普通请求')],
                            artifacts={'技能目录.json': catalog, '选中与普通_SSE.json': frames + tail})

    asyncio.run(main())


def test_stop_drains_the_inflight_write_without_dispatching_tail(app, agentcore_paths, agentcore_evidence, monkeypatch):
    async def main():
        started = asyncio.Event()
        release = Event()
        dispatched = []
        execute = registry.execute
        loop = asyncio.get_running_loop()
        async with web(app) as client:
            seed_demo_history(agentcore_paths)
            tx_id = rows('SELECT id FROM transactions_raw ORDER BY id')[0][0]
            first = tool('keep', APPLY, {'op': 'init', 'tx_ids': [tx_id], 'plan': {}})
            tail = tool('skip', APPLY, {'op': 'uninstall', 'tx_ids': [tx_id]})
            requests = []

            async def model(_cfg, **request):
                requests.append(request)
                assert len(requests) == 1
                return call_reply(first, tail)

            def block(name, args, context):
                dispatched.append(args['op'])
                if len(dispatched) == 1:
                    loop.call_soon_threadsafe(started.set)
                    assert release.wait(12), '测试必须释放在途操作'
                return execute(name, args, context)

            monkeypatch.setattr(adapter, 'complete', model)
            monkeypatch.setattr(registry, 'execute', block)
            evidence = Evidence(agentcore_evidence, agentcore_paths, 'C_显式停止_保留在途写入')
            url = await create(client, '合成停止场景')
            try:
                run = await body(client.post(f'{url}/turns', json={'text': '初始化示例，再尝试卸载。'}), 202)
                path = f"{url}/turns/{run['id']}"
                await asyncio.wait_for(started.wait(), 5)
                assert rows('SELECT * FROM ledger2_tx') == []
                stopped = await body(client.post(f'{path}/stop', json={}))
                assert stopped['state'] == 'stopping' and stopped['end_id'] is None
                assert rows('SELECT * FROM agentcore_ledger_receipt') == []
                evidence.step('工具批 `init` / `uninstall`；首项已派发但阻塞时 POST `/stop`',
                              '分清停止派发和回滚已派发写操作。', '响应 `stopping`；此刻还没有 V2 写入或执行回执。')
                release.set()
                frames = await events(client, path, state='stopped', code='USER_STOPPED')
                receipt, skipped = results(frames).values()
                assert dispatched == ['init'] and receipt['result']['state'] == 'executed' and not receipt['is_error']
                assert skipped['is_error'] and skipped['result']['error']['code'] == 'STOPPED_BEFORE_CALL'
                stored_receipt, = rows('SELECT result_json FROM agentcore_ledger_receipt')
                assert {'ok': True, **json.loads(stored_receipt[0])} == receipt['result']
                assert rows('SELECT tx_id FROM ledger2_tx') == [(tx_id,)]
                assert rows('SELECT COUNT(*) FROM ledger2_op_log') == [(1,)]
                detail = await body(client.get(url))
                check_detail(detail, run, frames)
                evidence.step('放行在途 `init`；读取全部 SSE 和新 V2 树', '验证停止后不派发卸载，也不丢真实回执。',
                              f'仅派发 `init`，交易 {tx_id} 仍已初始化；回执与事务表一致；卸载标为 `STOPPED_BEFORE_CALL`，终态 `stopped`。')
                evidence.finish(await body(client.get(TREE)), [(run['id'], '停止的一轮'), (tx_id, '在途初始化交易')],
                                artifacts={'SSE.json': frames, '终态会话.json': detail})
            finally:
                release.set()

    asyncio.run(main())


def test_startup_closes_unfinished_batch_from_real_receipts_without_replay(app, agentcore_paths, agentcore_evidence, monkeypatch):
    async def main():
        async with web(app) as client:
            evidence = Evidence(agentcore_evidence, agentcore_paths, 'D_启动恢复_查回执不重放')
            url = await create(client, '合成中断场景')
            sid = url.rsplit('/', 1)[1]
            import_url = await create(client, '合成入库中断场景')
            import_sid = import_url.rsplit('/', 1)[1]
            stage = await body(client.post(f'{AC}/upload', files={'file': (DEMO_FILENAME, DEMO_CSV, 'text/csv')}))

            def persist_inflight(sid, calls, dispatched):
                run = session.begin_run(sid, provider='openai_compatible', model='agentcore-scripted', system='', tools=[],
                                        digest='synthetic-restart', snapshot='', text='执行并重读合成账本。')
                batch = [{'id': c.id, 'name': c.name, 'arguments': c.arguments} for c in calls]
                aid = session.append(sid, 'assistant', 'assistant_text', {'tool_calls': batch, 'step': 1})
                for c in calls[:dispatched]:
                    session.append(sid, 'system', 'tool_started', {'assistant_id': aid, 'tool_call_id': c.id,
                                                                  'name': c.name, 'step': 1})
                return run, registry.ToolContext(session_id=sid, call_key=f"{run['id']}:{aid}:0")

            # 不构造「第一批没结果却已开始第二批」这种实际循环不会产生的日志。
            # 分开两轮，分别在导入提交和 V2 提交处制造断电窗口。
            first = tool('import', COMMIT, {'stage_id': stage['stage_id']})
            import_run, ctx = persist_inflight(import_sid, [first], 1)
            committed, is_error = registry.execute(first.name, json.loads(first.arguments), ctx)
            assert not is_error and committed['receipt']['inserted'] == 3
            batch_id = committed['receipt']['batch_id']
            assert not import_tool._row_path(stage['stage_id']).exists()
            ids = [t[0] for t in rows('SELECT id FROM transactions_raw WHERE import_batch=? ORDER BY id', (batch_id,))]
            init = tool('init', APPLY, {'op': 'init', 'tx_ids': ids, 'new_node': {'name': '恢复示例'}})
            items = [init, tool('tail', APPLY, {'op': 'uninstall', 'tx_ids': ids})]
            run, ctx = persist_inflight(sid, items, 1)
            applied, is_error = registry.execute(init.name, json.loads(init.arguments), ctx)
            assert not is_error and applied['state'] == 'executed'
            read_url = await create(client, '合成读取中断场景')
            read_run, _ = persist_inflight(read_url.rsplit('/', 1)[1], [tool('missing', QUERY, {'sql': 'SELECT 1'})], 1)
            assert rows("SELECT kind FROM agentcore_message WHERE kind='tool_result'") == []
            evidence.checkpoint('写入已提交_消息未记录')
            evidence.step('用真事务制造「业务提交、tool_result 未落盘」窗口',
                          '不杀实际服务，用合成库复刻重启时看到的日志和业务回执。',
                          f'批次 `{batch_id}` 已新增 3 笔；V2 初始化已提交；日志无写入结果。另测已派发无回执的读和未派发的卸载。')
            before = {table: rows(f'SELECT * FROM {table}') for table in (
                'transactions_raw', 'import_batches', 'ledger2_tx', 'ledger2_op_log', 'agentcore_ledger_receipt',
            )}

        attempted = []

        def no_execute(*args, **kwargs):
            attempted.append('tool')
            raise AssertionError('启动恢复不应执行工具')

        async def no_model(*args, **kwargs):
            attempted.append('model')
            raise AssertionError('启动恢复不应调用模型')

        monkeypatch.setattr(registry, 'execute', no_execute)
        monkeypatch.setattr(adapter, 'complete', no_model)
        async with web(app) as client:
            detail = await body(client.get(url))
            frames = await events(client, f"{url}/turns/{run['id']}", state='interrupted', code='PROCESS_INTERRUPTED')
            check_detail(detail, run, frames)
            items = results(frames)
            assert list(items) == ['init', 'tail']
            assert items['init']['recovered'] and items['init']['result'] == applied
            assert items['tail']['is_error'] and items['tail']['result']['error']['code'] == 'INTERRUPTED_BEFORE_CALL'
            import_frames = await events(client, f"{import_url}/turns/{import_run['id']}",
                                         state='interrupted', code='PROCESS_INTERRUPTED')
            import_items = results(import_frames)
            assert list(import_items) == ['import']
            assert import_items['import']['recovered'] and import_items['import']['result'] == committed
            read_frames = await events(client, f"{read_url}/turns/{read_run['id']}",
                                       state='interrupted', code='PROCESS_INTERRUPTED')
            read_items = results(read_frames)
            assert list(read_items) == ['missing']
            assert read_items['missing']['is_error']
            assert read_items['missing']['result']['error']['code'] == 'RECEIPT_UNAVAILABLE'
            for session_id, expected in ((sid, items), (import_sid, import_items),
                                         (read_url.rsplit('/', 1)[1], read_items)):
                projected = session.derive(session.events(session_id))
                actual_tools = [(m['tool_call_id'], json.loads(m['content'])) for m in projected if m['role'] == 'tool']
                assert actual_tools == [(key, value['result']) for key, value in expected.items()]
            assert not attempted and all(rows(f'SELECT * FROM {t}') == data for t, data in before.items())
            final_tree = await body(client.get(TREE))
            assert final_tree['tree']['count'] == 3 and final_tree['uninit'] == 0
        async with web(app) as client:
            assert await body(client.get(url)) == detail and not attempted
        evidence.step('重走 startup 两次，GET 会话和 SSE', '核对恢复不是重试，已经闭合的批次也不会再次写。',
                      '原样恢复 1 条导入与 1 条 V2 回执，分别标明无回执和未派发；终态 `interrupted`。'
                      '模型／执行入口调用 0 次，流水、批次、账本操作和事务回执不变。再次冷启结果相同。')
        evidence.finish(final_tree, [(run['id'], '被恢复的 V2 轮'), (import_run['id'], '被恢复的导入轮'),
                                     (read_run['id'], '被恢复的只读轮'), (batch_id, '已提交并复用的批次'),
                                     (applied['created']['nodes']['new'], '已提交并复用的 V2 节点')],
                        artifacts={'恢复_SSE.json': frames, '导入恢复_SSE.json': import_frames,
                                   '只读恢复_SSE.json': read_frames, '恢复后会话.json': detail})

    asyncio.run(main())


def test_mcp_cold_start_over_stdio(agentcore_paths, agentcore_evidence):
    import sys
    from pathlib import Path

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from backend.ledger2.service import get_tree

    root = Path(__file__).resolve().parents[1]
    db.init_db()
    prior = seed_demo_history(agentcore_paths)
    assert not rows("SELECT name FROM sqlite_schema WHERE name IN ('agentcore_tx','ledger2_node')")
    # 只重定向子进程的路径；用 run_module 进入实际 __main__ stdio 冷启动分支。
    bootstrap = (
        'import os, runpy; from pathlib import Path; from backend import db; '
        'root = Path(os.environ["SYNTHETIC_DB_DIR"]); '
        'db.DB_DIR = root; db.DB_PATH = root / "synthetic.db"; db.SEED_PATH = root / "no_seed"; '
        'runpy.run_module("backend.mcp.server", run_name="__main__")'
    )
    params = StdioServerParameters(command=sys.executable, args=['-X', 'utf8', '-c', bootstrap], cwd=root, env={
        'SYNTHETIC_DB_DIR': str(agentcore_paths), 'AGENTCORE_HOME': str(agentcore_paths / 'agentcore'),
        'AI_FINANCE_MASTER_KEY_FILE': str(agentcore_paths / 'unused.key'),
    })

    async def main():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=12)) as client:
                handshake = await client.initialize()
                assert handshake.serverInfo.name == 'ai-finance-ledger'
                tools = (await client.list_tools()).tools
                expected = registry.openai_tools(scope='business')
                assert 'finance_report_brief_write' in {t.name for t in tools}
                assert [(t.name, t.description, t.inputSchema) for t in tools] == [
                    (t['function']['name'], t['function']['description'], t['function']['parameters']) for t in expected
                ]
                evidence = Evidence(agentcore_evidence, agentcore_paths, 'E_MCP_stdio冷启动与直写')
                evidence.step('启动独立 stdio 子进程并发送 `initialize` / `tools/list`',
                              '不是内存 SDK 会话；从只有基础 schema 的临时库走真正入口。',
                              '握手成功，所有业务工具名称、描述、参数 schema 与 Web registry 完全一致。')

                async def call(name, args):
                    reply = await client.call_tool(name, args)
                    assert not reply.isError and reply.structuredContent['ok'], reply
                    return reply.structuredContent

                query = await call(QUERY, {'sql': 'SELECT id,amount,remark FROM transactions_raw ORDER BY id'})
                assert [r[1:] for r in query['rows']] == [[8.0, '预置重复项'], [9.0, '非本批']]
                tx_id = query['rows'][0][0]
                init = await call(APPLY, {'op': 'init', 'tx_ids': [tx_id], 'plan': {}})
                assert init['state'] == 'executed' and init['tx_count'] == 1
                view = await call('finance_ledger_read', {'mode': 'summary'})
                assert (view['root']['e'], view['root']['i'], view['uninit']) == (800, 0, 1)
                assert rows('SELECT tx_id FROM ledger2_tx') == [(tx_id,)]
                assert rows('SELECT COUNT(*) FROM agentcore_action') == [(0,)]
                assert rows('SELECT COUNT(*) FROM agentcore_run') == [(0,)]
                with closing(db.get_conn()) as conn:
                    tree = get_tree(conn)
                assert (tree['tree']['e'], tree['tree']['i']) == (800, 0)
                evidence.step('stdio 查询原始字段 → 初始化单笔 → 重读 V2', '独立进程用真实业务读写，确认没有改走旧审批流程。',
                              f'读到两笔原始金额及备注；只初始化交易 {tx_id}，根 `e=800, i=0`，另 1 笔未初始化，未生成旧卡。')
                evidence.finish(tree, [(prior, '合成导入批次'), (tx_id, 'stdio 初始化的交易')], artifacts={
                    '握手.json': handshake.model_dump(mode='json'), '流水.json': query, '执行回执.json': init, '重读.json': view,
                })

    asyncio.run(asyncio.wait_for(main(), 45))
