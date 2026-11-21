"""五类 AgentCore 长任务：真实 HTTP 路由、真实账本事务、本地脚本模型。

脚本模型只按工具回执决定下一步，不伪装成真实模型的理解能力；界面另有测试模式标识。
每个场景单独一套临时库和证据目录，绝不碰 db/bills.db。
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager, closing

import httpx
import pytest

from agentcore_evidence import Evidence
from agentcore_support import DEMO_CSV, DEMO_FILENAME, call_reply, install_config, tool
from backend import db
from backend.agentcore import adapter, runtime
from backend.ledger2 import ops, presets, store as ledger_store
from ledger2_support import PLATFORM_ID, PLATFORM_NAME, add_tx, reset_log

AC = '/api/agentcore'
TREE = '/api/ledger2/tree'
QUERY = 'finance_query_transactions'
APPLY = 'finance_ledger_apply'
READ = 'finance_ledger_read'
COMMIT = 'finance_import_commit'


# ── 真实的 ASGI 应用、SSE 与会话 ────────────────────────────────────────────

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


async def events(client, path, *, state='completed', code=None):
    response = await asyncio.wait_for(client.get(f'{path}/events', params={'after': 0}), 15)
    assert response.status_code == 200 and response.headers['content-type'].startswith('text/event-stream')
    frames = frames_from(response.text)
    last = frames[-1]
    assert last['type'] == 'terminal' and last['item']['run_id'] == path.rsplit('/', 1)[1], frames
    assert (last['item']['state'], last['item']['code']) == (state, code), last
    return frames


def tool_items(frames):
    return {f['item']['call_id']: f['item'] for f in frames if f.get('item', {}).get('type') == 'tool'}


def assistant_texts(frames):
    return [f['item']['text'] for f in frames if f.get('item', {}).get('type') == 'assistant']


def all_ok(items):
    assert items and all(not item['is_error'] for item in items.values()), items


# ── 合成库 ──────────────────────────────────────────────────────────────────

def seed(rows, *, init=True, plan=None):
    """建库、插入虚构流水、按 plan（缺省为平台分类预设）初始化并清空撤销栈。"""
    db.init_db()
    conn = ledger_store.connect(db.DB_PATH)
    ledger_store.ensure_schema(conn)
    conn.execute('INSERT OR IGNORE INTO platforms(id, name, kind, adapter) VALUES(?,?,?,?)',
                 (PLATFORM_ID, PLATFORM_NAME, 'wallet', 'generic_csv'))
    ids = [add_tx(conn, **row) for row in rows]
    conn.commit()
    if init and ids:
        op = plan(conn, ids) if plan else presets.platform_category_op(conn, ids)
        ops.init_txs(conn, ids, op)
        reset_log(conn)
    conn.close()
    return ids


def plan_meals(conn, ids):
    """正餐 / 零食两个用户节点，另有一批被误分到「教育」的饮食和一笔真教育支出。"""
    return {
        'nodes': [
            {'ref': 'meal', 'name': '正餐', 'id': 'n-meal'},
            {'ref': 'snack', 'name': '零食', 'id': 'n-snack'},
            {'ref': 'edu', 'name': '教育', 'id': 'cat:教育'},
        ],
        'place': [
            {'node': 'meal', 'txs': [1, 2, 3]},
            {'node': 'snack', 'txs': [4, 5]},
            {'node': 'edu', 'txs': [6, 7, 8, 9]},
        ],
    }


def plan_with_external(conn, ids):
    """平台分类预设 + 一个根下空节点「花呗」，用于接收分裂镜像。"""
    op = presets.platform_category_op(conn, ids)
    op['nodes'].append({'ref': 'hua', 'name': '花呗'})
    return op


# ── 脚本模型共用 ────────────────────────────────────────────────────────────

def _results(messages):
    out = {}
    for message in messages:
        if message['role'] == 'tool':
            value = json.loads(message['content'])
            assert value['ok'], value
            out[message['tool_call_id']] = value
    return out


def _current(messages, goals):
    """定位当前轮的目标消息，并只收集它之后的工具回执。"""
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message['role'] == 'user' and message.get('content') in goals:
            return message['content'], _results(messages[index + 1:])
    raise AssertionError(f'模型历史里没有当前目标：{goals}')


def _stage_from_context(messages):
    for message in messages:
        content = message.get('content') or ''
        if message['role'] == 'user' and '<turn_context>' in content:
            for line in content.splitlines():
                if line.startswith('本页附件曾解析成功'):
                    return json.loads(line.split('：', 1)[1])['stage_id']
    raise AssertionError('当前轮 <turn_context> 没有 staged_import')


def _quote(value):
    return "'" + value.replace("'", "''") + "'"


def _node_by_name(done, key, name):
    node, = [item for item in done[key]['top_level'] if item['name'] == name]
    return node


# ── 场景 A：完全空库 ────────────────────────────────────────────────────────

A_GOAL1 = '我想把支付宝账单导进来，然后按平台把新账入到账本里。'
A_GOAL2 = '已经传到「导入账单」里了，继续。'
A_REPLY1 = (
    '账本里现在还没有任何流水。请在本页右侧「导入账单」面板点「选择文件」，选好账单后点「上传并解析」；'
    '解析成功后再回来跟我说一声，我会接着把它入库并按平台分类入账。解压密码只填在那个表单里，不要发在对话里。'
)


async def model_a(_cfg, *, messages, **_):
    goal, done = _current(messages, {A_GOAL1, A_GOAL2})
    if goal == A_GOAL1:
        return adapter.ModelReply(text=A_REPLY1)

    stage_id = _stage_from_context(messages)
    if 'stage' not in done:
        return call_reply(tool('skill', 'read_skill', {'skill_id': 'finance-import'}),
                          tool('stage', 'finance_import_read', {'mode': 'stage', 'stage_id': stage_id}))
    assert done['stage']['stage']['stage_id'] == stage_id
    if 'commit' not in done:
        return call_reply(tool('commit', COMMIT, {'stage_id': stage_id}))
    receipt = done['commit']['receipt']
    if 'candidates' not in done:
        return call_reply(tool('candidates', QUERY, {'sql': (
            'SELECT a.tx_id, a.category, a.amount_cents FROM agentcore_tx a '
            'JOIN transactions_raw r ON r.id=a.tx_id '
            f"WHERE a.initialized=0 AND r.import_batch={_quote(receipt['batch_id'])} ORDER BY a.tx_id"
        )}))
    tx_ids = [row[0] for row in done['candidates']['rows']]
    assert len(tx_ids) == receipt['inserted']
    if 'init' not in done:
        return call_reply(tool('init', APPLY, {'op': 'init', 'tx_ids': tx_ids, 'style': 'platform'}))
    if 'summary' not in done:
        return call_reply(tool('summary', READ, {'mode': 'summary'}))
    summary, root = done['summary'], done['summary']['root']
    categories = '、'.join(f"{item['name']} {item['count']} 笔" for item in summary['top_level'])
    return adapter.ModelReply(text=(
        f"导入完成：新增 {receipt['inserted']} 笔、跳过 {receipt['skipped']} 笔、失败 {receipt['failed']} 笔。"
        f"已按平台分类入账：{categories}；根节点支出 ¥{root['e'] / 100:.2f}、收入 ¥{root['i'] / 100:.2f}，"
        f"未初始化 {summary['uninit']} 笔。下一步可以让我挑几笔合并成净额组，或者修改某笔的标题。"
    ))


def test_a_empty_db_guides_upload_then_imports_and_initializes(app, agentcore_paths, agentcore_evidence, monkeypatch):
    monkeypatch.setattr(adapter, 'complete', model_a)

    async def main():
        async with web(app) as client:
            evidence = Evidence(agentcore_evidence, agentcore_paths, 'A_空库_上传导入初始化')
            url = await create(client, '空库导入')
            first = await body(client.post(f'{url}/turns', json={'text': A_GOAL1}), 202)
            first_frames = await events(client, f"{url}/turns/{first['id']}")
            assert tool_items(first_frames) == {}
            assert '「选择文件」' in assistant_texts(first_frames)[-1]
            assert '「上传并解析」' in assistant_texts(first_frames)[-1]
            empty = await body(client.get(TREE))
            assert (empty['tree']['count'], empty['uninit']) == (0, 0)
            evidence.step(f'空库第一轮：POST `/turns`\n\n> {A_GOAL1}',
                          '验证模型在没有附件时先指引本地上传表单，而不是向用户要路径或密码。',
                          '0 次工具调用；回复点名「选择文件」「上传并解析」；账本仍为空。')
            evidence.checkpoint('空库未上传')

            stage = await body(client.post(f'{AC}/upload', files={'file': (DEMO_FILENAME, DEMO_CSV, 'text/csv')}))
            assert (stage['state'], stage['total'], stage['failed']) == ('staged', 3, 0)
            second = await body(client.post(f'{url}/turns', json={
                'text': A_GOAL2,
                'page': {'route': '/v2/assistant', 'staged_import': {'stage_id': stage['stage_id'],
                                                                     'total': stage['total']}},
            }), 202)
            second_frames = await events(client, f"{url}/turns/{second['id']}")
            items = tool_items(second_frames)
            all_ok(items)
            assert list(items) == ['skill', 'stage', 'commit', 'candidates', 'init', 'summary']
            assert items['skill']['step'] == items['stage']['step']  # 同一批只读，可并行
            receipt = items['commit']['result']['receipt']
            assert (receipt['inserted'], receipt['skipped'], receipt['failed']) == (3, 0, 0)
            assert [row[1] for row in items['candidates']['result']['rows']] == ['餐饮', '其他收入', '交通']
            final = await body(client.get(TREE))
            assert (final['tree']['count'], final['tree']['e'], final['tree']['i'], final['uninit']) == (3, 4000, 1200, 0)
            names = {item['name']: item['count'] for item in final['tree']['children']}
            assert names == {'餐饮': 1, '其他收入': 1, '交通': 1}
            assert len(rows('SELECT * FROM transactions_raw')) == 3
            text = assistant_texts(second_frames)[-1]
            assert '新增 3 笔' in text and '未初始化 0 笔' in text
            evidence.step(f'上传后第二轮：POST `/turns`\n\n> {A_GOAL2}',
                          '验证附件只从页面快照取；读暂存、入库、取本批新增、初始化、重读串成一轮。',
                          f'6 次调用全部成功，前两次同一步（并行读）；批次新增 3 笔；根 e=4000、i=1200；三分类各 1 笔。')
            evidence.finish(final, [(stage['stage_id'], '上传暂存'), (receipt['batch_id'], '本次导入批次'),
                                    (_tree_node(final, '餐饮')['id'], '餐饮分类节点')],
                            artifacts={'第一轮_SSE.json': first_frames, '第二轮_SSE.json': second_frames,
                                       '会话.json': await body(client.get(url))})

    asyncio.run(main())


def rows(sql, params=()):
    with closing(db.get_conn()) as conn:
        return [tuple(row) for row in conn.execute(sql, params)]


# ── 场景 B：已导入、未初始化 ────────────────────────────────────────────────

B_ROWS = [
    dict(id=1, tx_time='2026-06-01 08:00:00', direction='支出', amount=12, tx_type='餐饮',
         counterparty='虚构早餐店', item='早餐', source='支付宝'),
    dict(id=2, tx_time='2026-06-02 12:00:00', direction='支出', amount=30, tx_type='餐饮',
         counterparty='虚构午饭馆', item='午饭', source='微信'),
    dict(id=3, tx_time='2026-06-03 19:00:00', direction='支出', amount=45, tx_type='餐饮',
         counterparty='虚构晚饭馆', item='晚饭', source='支付宝'),
    dict(id=4, tx_time='2026-06-04 08:00:00', direction='支出', amount=6, tx_type='交通',
         counterparty='虚构地铁', item='地铁', source='微信'),
    dict(id=5, tx_time='2026-06-05 15:00:00', direction='支出', amount=99, tx_type='购物',
         counterparty='虚构超市', item='日用品', source='银行'),
    dict(id=6, tx_time='2026-06-06 09:00:00', direction='收入', amount=500, tx_type='其他收入',
         counterparty='虚构朋友', item='红包', source='微信'),
]
B_GOAL = '把还没入账的流水按平台分类整理进账本，整理完告诉我各分类的笔数。'


async def model_b(_cfg, *, messages, **_):
    _goal, done = _current(messages, {B_GOAL})
    if 'detail' not in done:
        return call_reply(
            tool('dist', QUERY, {'sql': (
                'SELECT source, COUNT(*) AS n FROM agentcore_tx WHERE initialized=0 '
                'GROUP BY source ORDER BY source'
            )}),
            tool('detail', QUERY, {'sql': (
                'SELECT tx_id, source, category, direction, amount_cents FROM agentcore_tx '
                'WHERE initialized=0 ORDER BY tx_id'
            )}),
        )
    assert sum(row[1] for row in done['dist']['rows']) == len(done['detail']['rows']) == 6
    tx_ids = [row[0] for row in done['detail']['rows']]
    if 'init' not in done:
        return call_reply(tool('init', APPLY, {'op': 'init', 'tx_ids': tx_ids, 'style': 'platform'}))
    if 'summary' not in done:
        return call_reply(tool('summary', READ, {'mode': 'summary'}))
    summary, root = done['summary'], done['summary']['root']
    parts = '、'.join(f"{item['name']} {item['count']} 笔" for item in summary['top_level'])
    return adapter.ModelReply(text=(
        f"6 笔全部按平台分类入账：{parts}。根节点支出 ¥{root['e'] / 100:.2f}、"
        f"收入 ¥{root['i'] / 100:.2f}，未初始化 {summary['uninit']} 笔。下一步可以指定某几笔换到自建节点。"
    ))


def test_b_imported_db_initialize_by_platform(app, agentcore_paths, agentcore_evidence, monkeypatch):
    seed(B_ROWS, init=False)  # 只插流水，全部保持未初始化
    monkeypatch.setattr(adapter, 'complete', model_b)

    async def main():
        async with web(app) as client:
            evidence = Evidence(agentcore_evidence, agentcore_paths, 'B_已导入_按平台初始化')
            before = await body(client.get(TREE))
            assert (before['tree']['count'], before['uninit']) == (0, 6)
            url = await create(client, '按平台初始化')
            run = await body(client.post(f'{url}/turns', json={'text': B_GOAL}), 202)
            frames = await events(client, f"{url}/turns/{run['id']}")
            items = tool_items(frames)
            all_ok(items)
            assert list(items) == ['dist', 'detail', 'init', 'summary']
            assert items['dist']['step'] == items['detail']['step']
            tx_ids = [row[0] for row in items['detail']['result']['rows']]
            assert tx_ids == [1, 2, 3, 4, 5, 6]
            final = await body(client.get(TREE))
            assert (final['tree']['e'], final['tree']['i'], final['uninit']) == (19200, 50000, 0)
            counts = {item['name']: item['count'] for item in final['tree']['children']}
            assert counts == {'餐饮': 3, '交通': 1, '购物': 1, '其他收入': 1}
            assert '餐饮 3 笔' in assistant_texts(frames)[-1]
            evidence.step(f'已导入未初始化：POST `/turns`\n\n> {B_GOAL}',
                          '验证两条独立聚合先并行读，再按平台分类一次初始化，最后重读核对。',
                          '4 次调用；6 笔全部分类入账，餐饮 3、交通 1、购物 1、其他收入 1；根 e=19200、i=50000。')
            evidence.finish(final, [(item['id'], item['name']) for item in final['tree']['children']],
                            artifacts={'SSE.json': frames, '会话.json': await body(client.get(url))})

    asyncio.run(main())


# ── 场景 C：已初始化、按吃饭时间重分 ────────────────────────────────────────

C_ROWS = [
    dict(id=1, tx_time='2026-04-01 08:00:00', direction='支出', amount=10, tx_type='餐饮', item='包子'),
    dict(id=2, tx_time='2026-04-01 12:00:00', direction='支出', amount=25, tx_type='餐饮', item='盖饭'),
    dict(id=3, tx_time='2026-04-01 19:00:00', direction='支出', amount=30, tx_type='餐饮', item='面条'),
    dict(id=4, tx_time='2026-04-02 15:00:00', direction='支出', amount=8, tx_type='餐饮', item='薯片'),
    dict(id=5, tx_time='2026-04-02 22:00:00', direction='支出', amount=6, tx_type='餐饮', item='牛奶'),
    dict(id=6, tx_time='2026-04-03 12:30:00', direction='支出', amount=20, tx_type='教育', item='牛肉面'),
    dict(id=7, tx_time='2026-04-03 16:00:00', direction='支出', amount=15, tx_type='教育', item='咖啡'),
    dict(id=8, tx_time='2026-04-03 18:30:00', direction='支出', amount=60, tx_type='教育', item='火锅'),
    dict(id=9, tx_time='2026-04-04 09:00:00', direction='支出', amount=199, tx_type='教育', item='网课'),
]
C_GOAL = '饮食里有几笔被分到了教育。按早/午/晚饭时间把正餐归到「正餐」，其余归「零食」；不是吃饭的那笔留在教育。'
C_FOOD = {'牛肉面', '咖啡', '火锅'}


async def model_c(_cfg, *, messages, **_):
    _goal, done = _current(messages, {C_GOAL})
    if 'candidates' not in done:
        return call_reply(tool('candidates', QUERY, {'sql': (
            'SELECT a.tx_id, a.item, a.category, a.amount_cents, r.hour '
            'FROM agentcore_tx a JOIN transactions_raw r ON r.id=a.tx_id '
            "WHERE a.initialized=1 AND a.category='教育' ORDER BY a.tx_id"
        )}))
    if 'tree' not in done:
        return call_reply(tool('tree', READ, {'mode': 'summary'}))
    meal_id, snack_id = _node_by_name(done, 'tree', '正餐')['id'], _node_by_name(done, 'tree', '零食')['id']
    meals, snacks = [], []
    for tx_id, item, _category, _amount, hour in done['candidates']['rows']:
        if item not in C_FOOD:
            continue
        (meals if 5 <= hour <= 14 or 17 <= hour <= 21 else snacks).append(f'tx:{tx_id}')
    assert (meals, snacks) == (['tx:6', 'tx:8'], ['tx:7'])
    if 'move-meal' not in done:
        return call_reply(tool('move-meal', APPLY, {'op': 'move', 'items': meals, 'target': meal_id}),
                          tool('move-snack', APPLY, {'op': 'move', 'items': snacks, 'target': snack_id}))
    if 'after' not in done:
        return call_reply(tool('after', READ, {'mode': 'summary'}),
                          tool('edu', READ, {'mode': 'node', 'ref': 'cat:教育'}))
    summary = done['after']
    counts = {item['name']: item['count'] for item in summary['top_level']}
    assert counts == {'正餐': 5, '零食': 3, '教育': 1}
    assert done['edu']['node']['count'] == 1
    return adapter.ModelReply(text=(
        f"已重分：正餐 {counts['正餐']} 笔、零食 {counts['零食']} 笔，教育只剩网课 1 笔。"
        f"根节点支出 ¥{summary['root']['e'] / 100:.2f}、收入 ¥{summary['root']['i'] / 100:.2f}，"
        f"未初始化 {summary['uninit']} 笔。若时间规则还要调整，告诉我边界即可。"
    ))


def test_c_initialized_db_reclassify_meals_by_hour(app, agentcore_paths, agentcore_evidence, monkeypatch):
    seed(C_ROWS, plan=plan_meals)
    monkeypatch.setattr(adapter, 'complete', model_c)

    async def main():
        async with web(app) as client:
            evidence = Evidence(agentcore_evidence, agentcore_paths, 'C_已初始化_按时间重分饮食')
            before = await body(client.get(TREE))
            assert before['uninit'] == 0 and len(before['tree']['children']) == 3
            url = await create(client, '重分饮食')
            run = await body(client.post(f'{url}/turns', json={'text': C_GOAL}), 202)
            frames = await events(client, f"{url}/turns/{run['id']}")
            items = tool_items(frames)
            all_ok(items)
            assert list(items) == ['candidates', 'tree', 'move-meal', 'move-snack', 'after', 'edu']
            assert items['move-meal']['step'] == items['move-snack']['step']  # 同批两个写入按序执行
            assert [row[1] for row in items['candidates']['result']['rows']] == ['牛肉面', '咖啡', '火锅', '网课']
            final = await body(client.get(TREE))
            counts = {item['name']: item['count'] for item in final['tree']['children']}
            assert counts == {'正餐': 5, '零食': 3, '教育': 1}
            assert (final['tree']['e'], final['tree']['i'], final['uninit']) == (37300, 0, 0)
            meal = _tree_node(final, '正餐')
            assert (meal['e'], meal['i']) == (14500, 0)
            snack = _tree_node(final, '零食')
            assert (snack['e'], snack['i']) == (2900, 0)
            assert '正餐 5 笔' in assistant_texts(frames)[-1]
            evidence.step(f'已初始化：POST `/turns`\n\n> {C_GOAL}',
                          '验证脚本按真实 hour 判断早/午/晚餐，把误分到教育的饮食移回饮食节点。',
                          '6 次调用；牛肉面、火锅进正餐，咖啡进零食，网课留在教育；根金额不变。')
            evidence.finish(final, [('n-meal', '正餐'), ('n-snack', '零食'), ('cat:教育', '教育')],
                            artifacts={'SSE.json': frames, '会话.json': await body(client.get(url))})

    asyncio.run(main())


def _tree_node(tree, name):
    node, = [item for item in tree['tree']['children'] if item['name'] == name]
    return node


# ── 场景 D：有基础树，AA 回款抵消并挂新节点 ─────────────────────────────────

D_ROWS = [
    dict(id=1, tx_time='2026-05-01 20:00:00', direction='收入', amount=200, tx_type='其他收入',
         counterparty='虚构舍友甲', item='AA 回款'),
    dict(id=2, tx_time='2026-05-01 19:00:00', direction='支出', amount=600, tx_type='餐饮',
         counterparty='虚构饭店', item='聚餐'),
    dict(id=3, tx_time='2026-05-02 09:00:00', direction='支出', amount=50, tx_type='交通',
         counterparty='虚构出行', item='打车'),
]
D_GOAL = '舍友把 AA 的 200 转回来了，跟我那笔 600 的聚餐抵掉，再把这一组挂到「出去吃」。'
D_GROUP = 'AA 抵消'
D_NODE = '出去吃'


async def model_d(_cfg, *, messages, **_):
    _goal, done = _current(messages, {D_GOAL})
    if 'find' not in done:
        return call_reply(tool('find', QUERY, {'sql': (
            'SELECT tx_id, direction, amount_cents, category, item FROM agentcore_tx '
            "WHERE initialized=1 AND item IN ('AA 回款','聚餐') ORDER BY tx_id"
        )}))
    refund = next(row for row in done['find']['rows'] if row[1] == '收入')
    meal = next(row for row in done['find']['rows'] if row[1] == '支出')
    if 'read-refund' not in done:
        return call_reply(tool('read-refund', READ, {'mode': 'tx', 'ref': f'tx:{refund[0]}'}),
                          tool('read-meal', READ, {'mode': 'tx', 'ref': f'tx:{meal[0]}'}))
    target = done['read-meal']['parent_ref']
    if 'move' not in done:
        return call_reply(tool('move', APPLY, {'op': 'move', 'items': [f'tx:{refund[0]}'], 'target': target}))
    if 'group' not in done:
        return call_reply(tool('group', APPLY, {'op': 'create_group', 'name': D_GROUP,
                                                'items': [f'tx:{refund[0]}', f'tx:{meal[0]}']}))
    group_id = done['group']['created']['group_id']
    if 'node' not in done:
        return call_reply(tool('node', APPLY, {'op': 'create_node', 'name': D_NODE,
                                               'items': [f'g:{group_id}']}))
    if 'verify' not in done:
        return call_reply(tool('verify', READ, {'mode': 'node', 'ref': f'g:{group_id}'}))
    group = done['verify']['node']
    return adapter.ModelReply(text=(
        f"回款已移到「餐饮」，和 600 元聚餐组成净额组「{D_GROUP}」，并挂到新节点「{D_NODE}」下。"
        f"该组 2 名成员，支出 ¥{group['e'] / 100:.2f}、收入 ¥{group['i'] / 100:.2f}，"
        f"净支出 ¥{(group['e'] - group['i']) / 100:.2f}。下一步可以继续把其他 AA 回款也配进来。"
    ))


def test_d_existing_tree_aa_net_group_under_new_node(app, agentcore_paths, agentcore_evidence, monkeypatch):
    seed(D_ROWS)
    monkeypatch.setattr(adapter, 'complete', model_d)

    async def main():
        async with web(app) as client:
            evidence = Evidence(agentcore_evidence, agentcore_paths, 'D_有基础树_AA抵消建组与节点')
            before = await body(client.get(TREE))
            assert {item['name'] for item in before['tree']['children']} == {'其他收入', '交通', '餐饮'}
            url = await create(client, 'AA 抵消')
            run = await body(client.post(f'{url}/turns', json={'text': D_GOAL}), 202)
            frames = await events(client, f"{url}/turns/{run['id']}")
            items = tool_items(frames)
            all_ok(items)
            assert list(items) == ['find', 'read-refund', 'read-meal', 'move', 'group', 'node', 'verify']
            assert items['read-refund']['step'] == items['read-meal']['step']
            group_id = items['group']['result']['created']['group_id']
            node_id = items['node']['result']['created']['node_id']
            final = await body(client.get(TREE))
            group = _find_item(final['tree'], 'group', group_id)
            assert (group['e'], group['i'], group['count']) == (40000, 0, 2)
            assert {member['ref'] for member in group['members']} == {'tx:1', 'tx:2'}
            new_node = _find_item(final['tree'], 'node', node_id)
            assert new_node['name'] == D_NODE and [child['id'] for child in new_node['children']] == [group_id]
            assert (final['tree']['e'], final['tree']['i'], final['uninit']) == (45000, 0, 0)
            assert '净支出 ¥400.00' in assistant_texts(frames)[-1]
            evidence.step(f'有基础树：POST `/turns`\n\n> {D_GOAL}',
                          '验证先读两笔的父节点，跨节点先移动成同父，再建组、建新节点、重读核对。',
                          f'7 次调用；组 `{group_id}` 两成员 e=40000、i=0；新节点 `{node_id}` 只含该组；'
                          '组净支出 ¥400，根另含打车支出 ¥50。')
            evidence.finish(final, [(group_id, D_GROUP), (node_id, D_NODE), (1, 'AA 回款'), (2, '聚餐')],
                            artifacts={'SSE.json': frames, '会话.json': await body(client.get(url))})

    asyncio.run(main())


def _find_item(item, kind, id_):
    if item.get('type') == kind and item.get('id') == id_:
        return item
    for child in item.get('children', item.get('members', [])):
        found = _find_item(child, kind, id_)
        if found is not None:
            return found
    return None


# ── 场景 E：分裂、新账单初始化、撤销 ────────────────────────────────────────

E_ROWS = [
    dict(id=1, tx_time='2026-07-01 18:00:00', direction='支出', amount=300, tx_type='餐饮',
         counterparty='虚构饭店', item='聚餐'),
    dict(id=2, tx_time='2026-07-01 19:00:00', direction='支出', amount=80, tx_type='交通',
         counterparty='虚构出行', item='打车'),
]
E_HEADER = ('支付宝交易记录明细查询\n'
            '收/支,交易时间,交易分类,交易对方,商品说明,金额,收/付款方式,交易状态,交易订单号,商家订单号,备注\n')
E_CSV = (E_HEADER + '支出,2026-07-02 13:00:00,购物,虚构对方戊,示例台灯,50.00,模拟余额,交易成功,'
         'e-demo-5,,仅供测试\n').encode('utf-8-sig')
E_FILENAME = 'agentcore-new-bill.csv'
E_GOAL = ('先把我那笔 300 的聚餐分裂，镜像挂到「花呗」；再把我刚传的 50 元新账单导入并按平台初始化；'
          '最后撤销刚才的初始化，把撤销后的账本读数告诉我。')


async def model_e(_cfg, *, messages, **_):
    _goal, done = _current(messages, {E_GOAL})
    stage_id = _stage_from_context(messages)
    if 'stage' not in done:
        return call_reply(tool('skill', 'read_skill', {'skill_id': 'finance-import'}),
                          tool('stage', 'finance_import_read', {'mode': 'stage', 'stage_id': stage_id}))
    if 'split' not in done:
        return call_reply(tool('split', APPLY, {'op': 'split', 'tx_id': 1}))
    assert done['split']['created']['split'] == [1]
    if 'mirror' not in done:
        return call_reply(tool('mirror', READ, {'mode': 'tx', 'ref': 'txm:1'}))
    if 'tree' not in done:
        return call_reply(tool('tree', READ, {'mode': 'summary'}))
    hua = _node_by_name(done, 'tree', '花呗')
    if 'move' not in done:
        return call_reply(tool('move', APPLY, {'op': 'move', 'items': ['txm:1'], 'target': hua['id']}))
    if 'commit' not in done:
        return call_reply(tool('commit', COMMIT, {'stage_id': stage_id}))
    receipt = done['commit']['receipt']
    if 'candidates' not in done:
        return call_reply(tool('candidates', QUERY, {'sql': (
            'SELECT a.tx_id, a.category, a.amount_cents FROM agentcore_tx a '
            'JOIN transactions_raw r ON r.id=a.tx_id '
            f"WHERE a.initialized=0 AND r.import_batch={_quote(receipt['batch_id'])} ORDER BY a.tx_id"
        )}))
    tx_ids = [row[0] for row in done['candidates']['rows']]
    assert len(tx_ids) == receipt['inserted'] == 1
    if 'init' not in done:
        return call_reply(tool('init', APPLY, {'op': 'init', 'tx_ids': tx_ids, 'style': 'platform'}))
    if 'pre-undo' not in done:
        return call_reply(tool('pre-undo', READ, {'mode': 'summary'}))
    applied = [item for item in done['pre-undo']['status']['history'] if item['state'] == 'applied']
    if 'undo' not in done:
        return call_reply(tool('undo', APPLY, {'op': 'undo', 'expected_seq': applied[-1]['seq']}))
    if 'after' not in done:
        return call_reply(tool('after', READ, {'mode': 'summary'}))
    summary = done['after']
    hua_after = _node_by_name(done, 'after', '花呗')
    return adapter.ModelReply(text=(
        f"分裂已完成：原 300 元聚餐留在餐饮，镜像在「花呗」（该节点支出 ¥{hua_after['e'] / 100:.2f}、"
        f"收入 ¥{hua_after['i'] / 100:.2f}）。新账单 {receipt['inserted']} 笔已导入并初始化，随后按 seq="
        f"{applied[-1]['seq']} 撤销了那次初始化。现在未初始化 {summary['uninit']} 笔，"
        f"根节点支出 ¥{summary['root']['e'] / 100:.2f}、收入 ¥{summary['root']['i'] / 100:.2f}。"
        '若还要保留刚撤销的初始化，可以让我重做。'
    ))


def test_e_split_import_init_then_undo(app, agentcore_paths, agentcore_evidence, monkeypatch):
    seed(E_ROWS, plan=plan_with_external)
    monkeypatch.setattr(adapter, 'complete', model_e)

    async def main():
        async with web(app) as client:
            evidence = Evidence(agentcore_evidence, agentcore_paths, 'E_组合_分裂新账单撤销')
            stage = await body(client.post(f'{AC}/upload', files={'file': (E_FILENAME, E_CSV, 'text/csv')}))
            assert stage['total'] == 1
            before = await body(client.get(TREE))
            assert before['uninit'] == 0
            url = await create(client, '分裂新账单撤销')
            run = await body(client.post(f'{url}/turns', json={
                'text': E_GOAL,
                'page': {'route': '/v2/assistant', 'staged_import': {'stage_id': stage['stage_id'],
                                                                     'total': stage['total']}},
            }), 202)
            frames = await events(client, f"{url}/turns/{run['id']}")
            items = tool_items(frames)
            all_ok(items)
            assert list(items) == ['skill', 'stage', 'split', 'mirror', 'tree', 'move', 'commit',
                                   'candidates', 'init', 'pre-undo', 'undo', 'after']
            assert items['skill']['step'] == items['stage']['step']
            assert items['mirror']['result']['parent_ref'] == 'cat:餐饮'
            receipt = items['commit']['result']['receipt']
            assert (receipt['inserted'], receipt['skipped']) == (1, 0)
            new_id, = [row[0] for row in items['candidates']['result']['rows']]
            assert items['undo']['result']['op'] == 'undo'
            final = await body(client.get(TREE))
            assert (final['tree']['e'], final['tree']['i'], final['uninit']) == (38000, 30000, 1)
            assert [item['name'] for item in final['tree']['children']].count('花呗') == 1
            assert _tree_node(final, '花呗')['count'] == 1
            assert _tree_node(final, '餐饮')['count'] == 1
            assert rows('SELECT tx_id FROM ledger2_tx WHERE tx_id=?', (new_id,)) == []
            assert '未初始化 1 笔' in assistant_texts(frames)[-1]
            evidence.step(f'组合场景：上传 → POST `/turns`\n\n> {E_GOAL}',
                          '验证分裂回执取镜像、镜像跨节点移动、新账单三步导入、读栈顶后指定 seq 撤销。',
                          f'12 次调用；镜像进花呗，餐饮仍 300；新批次 `{receipt["batch_id"]}` 新增 1 笔后撤销初始化，'
                          f'最终未初始化 1 笔、根 e=38000、i=30000。')
            evidence.finish(final, [(stage['stage_id'], '上传暂存'), (receipt['batch_id'], '新账单批次'),
                                    (new_id, '撤销初始化后的新交易'),
                                    (_tree_node(final, '花呗')['id'], '接收镜像的节点')],
                            artifacts={'SSE.json': frames, '会话.json': await body(client.get(url))})

    asyncio.run(main())
