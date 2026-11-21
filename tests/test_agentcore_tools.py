"""能力层契约：全字段流水、V2 直写、导入事务、以及仍由用户处理的旧卡。"""
from __future__ import annotations

import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Barrier

import pytest

from agentcore_support import DEMO_CSV, DEMO_FILENAME
from backend import db
from backend.agentcore import actions, import_tool, registry, sql_tool
from backend.agentcore.registry import ToolContext
from backend.importer.base import Row
from ledger2_support import DATA, make_db, snapshot

QUERY = 'finance_query_transactions'
READ = 'finance_ledger_read'
APPLY = 'finance_ledger_apply'
COMMIT = 'finance_import_commit'


def result(tool, args, *, key=None):
    value, is_error = registry.execute(tool, args, ToolContext(call_key=key))
    assert not is_error and value['ok'], value
    return value


def error(tool, args, *, key=None):
    value, is_error = registry.execute(tool, args, ToolContext(call_key=key))
    assert is_error and not value['ok'], value
    return value['error']['code']


def rows(sql, params=()):
    with closing(db.get_conn()) as conn:
        return [tuple(row) for row in conn.execute(sql, params)]


def ledger_rows():
    with closing(db.get_conn()) as conn:
        return snapshot(conn)


@pytest.fixture
def raw_db(agentcore_ready):
    make_db(db.DB_PATH, init=False).close()


@pytest.fixture
def ready_db(agentcore_ready):
    make_db(db.DB_PATH, init=True).close()


@pytest.fixture
def staged(agentcore_ready):
    upload = agentcore_ready / DEMO_FILENAME
    upload.write_bytes(DEMO_CSV)
    before = rows('SELECT * FROM transactions_raw ORDER BY id')
    stage = import_tool.stage_file(upload, upload.name)
    assert stage['state'] == 'staged' and stage['total'] == 3
    assert rows('SELECT * FROM transactions_raw ORDER BY id') == before
    return stage


def test_sql_reads_raw_and_distinct_analysis_units(raw_db):
    with closing(db.get_conn()) as conn, conn:
        conn.execute("UPDATE transactions_raw SET tx_no='fake-tx',merchant_no='fake-order',remark='合成备注',"
                     "pay_method='虚构余额',raw_json=? WHERE id=1", ('{"example":true}',))
        conn.execute("UPDATE transactions_raw SET direction='中性',amount=50.01,tx_type='  ' WHERE id=8")
        conn.execute("INSERT INTO tx_overrides(tx_id,manual_category) VALUES(1,'手工示例'),(3,'  ')")
        conn.execute('INSERT INTO tx_deletions(tx_id) VALUES(5)')
    result(APPLY, {'op': 'init', 'tx_ids': [1], 'plan': {}})
    raw = result(QUERY, {'sql': 'SELECT * FROM transactions_raw WHERE id=1'})
    fields = dict(zip(raw['columns'], raw['rows'][0]))
    with closing(db.get_conn()) as conn:
        assert fields == dict(conn.execute('SELECT * FROM transactions_raw WHERE id=1').fetchone())
    assert fields['amount'] == 30.0 and fields['remark'] == '合成备注'
    assert (fields['pay_method'], fields['tx_no'], fields['merchant_no'], json.loads(fields['raw_json'])) == (
        '虚构余额', 'fake-tx', 'fake-order', {'example': True},
    )
    view = result(QUERY, {'sql': 'SELECT tx_id,direction,amount_cents,category,initialized FROM agentcore_tx ORDER BY tx_id'})
    assert [row for row in view['rows'] if row[0] in (1, 3, 5, 8)] == [
        [1, '支出', 3000, '手工示例', 1], [3, '支出', 2000, '餐饮', 0], [8, '中性', 5001, '其他', 0],
    ]
    assert view['coverage'] == {
        'transactions_raw': {'date_from': '2026-01-05', 'date_to': '2026-02-03', 'total': 7},
        'agentcore_tx': {'date_from': '2026-01-05', 'date_to': '2026-02-02', 'total': 6},
    }
    empty = result(QUERY, {'sql': "SELECT SUM(amount_cents) FROM agentcore_tx WHERE date='2099-01-01'"})
    assert empty['rows'] == [[None]] and empty['coverage'] == view['coverage']


@pytest.mark.parametrize(('sql', 'code'), [
    ('SELECT * FROM ledger2_tx', 'SQL_ERROR'),
    ('SELECT * FROM sqlite_master', 'NOT_AUTHORIZED'),
    ('DELETE FROM transactions_raw', 'ONLY_SELECT'),
    ("SELECT load_extension('synthetic')", 'NOT_AUTHORIZED'),
    ('SELECT 1; SELECT 2', 'SQL_ERROR'),
])
def test_sql_boundary_does_not_change_source(raw_db, sql, code):
    before = rows('SELECT * FROM transactions_raw ORDER BY id')
    assert error(QUERY, {'sql': sql}) == code
    assert rows('SELECT * FROM transactions_raw ORDER BY id') == before


def test_sql_row_and_byte_limits_keep_complete_rows(agentcore_ready, monkeypatch):
    many = [{**DATA[0], 'id': i, 'item': f'虚构项{i}'} for i in range(1, 502)]
    make_db(db.DB_PATH, rows=many, init=False).close()
    data = result(QUERY, {'sql': 'SELECT tx_id FROM agentcore_tx ORDER BY tx_id'})
    assert data['row_count'] == 500 and data['truncated'] and data['rows'][-1] == [500]
    assert data['coverage']['agentcore_tx']['total'] == 501
    monkeypatch.setattr(sql_tool, 'MAX_BYTES', 2_500)
    with closing(db.get_conn()) as conn, conn:
        conn.executemany('UPDATE transactions_raw SET remark=? WHERE id=?', [('x' * 850, 1), ('y' * 850, 2), ('z' * 1600, 3)])
    data = result(QUERY, {'sql': 'SELECT id,remark FROM transactions_raw WHERE id<=3 ORDER BY id'})
    assert data['rows'] == [[1, 'x' * 850]] and data['truncated']
    assert error(QUERY, {'sql': 'SELECT remark FROM transactions_raw WHERE id=3'}) == 'ROW_TOO_LARGE'


def test_sql_interrupts_a_long_read(raw_db, monkeypatch):
    monkeypatch.setattr(sql_tool, 'MAX_SECONDS', 0.1)
    started = time.monotonic()
    assert error(QUERY, {'sql': 'WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) SELECT SUM(x) FROM c'}) == 'TIMEOUT'
    assert time.monotonic() - started >= 0.09
    assert result(QUERY, {'sql': 'SELECT COUNT(*) FROM agentcore_tx'})['rows'] == [[7]]


@pytest.mark.parametrize('bad', [[], [True], [1.0], ['1']])
def test_init_rejects_empty_selection_and_non_integer_ids(raw_db, bad):
    assert error(APPLY, {'op': 'init', 'tx_ids': bad}) == 'BAD_ARGUMENTS'
    assert rows('SELECT * FROM ledger2_tx') == []


def test_direct_init_resolves_all_explicitly_and_returns_real_node(raw_db):
    assert error(APPLY, {'op': 'init'}) == 'MISSING_FIELD'
    assert error(APPLY, {'op': 'init', 'tx_ids': [1], 'all_uninitialized': True}) == 'BAD_OP'
    first = result(APPLY, {'op': 'init', 'tx_ids': [1], 'new_node': {'name': '示例餐饮'}})
    node_id = first['created']['nodes']['new']
    assert first['state'] == 'executed' and first['seq'] == 1 and first['tx_count'] == 1
    assert rows('SELECT node_id FROM ledger2_tx WHERE tx_id=1') == [(node_id,)]
    rest = result(APPLY, {'op': 'init', 'all_uninitialized': True, 'target': node_id})
    assert rest['tx_count'] == 6
    assert error(APPLY, {'op': 'init', 'all_uninitialized': True}) == 'NO_EFFECT'
    node = result(READ, {'mode': 'node', 'ref': node_id})
    assert node['node']['count'] == 7 and node['node']['e'] == 22500 and node['node']['i'] == 805000
    assert rows('SELECT * FROM agentcore_action') == []


def test_default_init_uses_platform_categories(raw_db):
    result(APPLY, {'op': 'init', 'tx_ids': [1, 2]})
    assert rows("SELECT name FROM ledger2_node WHERE id LIKE 'cat:%' ORDER BY name") == [('工资',), ('餐饮',)]


def test_ledger_read_pages_full_items_without_recalculating_amounts(raw_db):
    result(APPLY, {'op': 'init', 'tx_ids': [1, 6], 'plan': {}})
    result(APPLY, {'op': 'update_manual', 'tx_id': 1, 'title': '合成标题', 'note': '字' * 15_000 + '末尾'})
    group = result(APPLY, {'op': 'create_group', 'items': ['tx:1', 'tx:6'], 'name': '抵扣', 'note': '合成组备注'})
    ref = 'g:' + group['created']['group_id']
    first = result(READ, {'mode': 'node', 'ref': ref, 'limit': 2})
    assert first['total'] == 2 and first['next_offset'] == 1  # 第一项单独就超过软字节预算
    tail = result(READ, {'mode': 'node', 'ref': ref, 'offset': first['next_offset']})
    assert tail['next_offset'] is None and first['node'] == tail['node']
    assert (tail['node']['e'], tail['node']['i'], tail['node']['note']) == (0, 2000, '合成组备注')
    items = {item['ref']: item for item in first['items'] + tail['items']}
    assert items['tx:1']['memo'].endswith('末尾') and len(items['tx:1']['memo']) == 15_002
    tx = result(READ, {'mode': 'tx', 'ref': 'tx:1'})
    assert tx['parent_ref'] == ref and tx['tx'] == items['tx:1']
    january_5 = result(READ, {'mode': 'node', 'ref': ref, 'date_from': '2026-01-05', 'date_to': '2026-01-05'})
    assert january_5['total'] == 1 and (january_5['node']['e'], january_5['node']['i']) == (3000, 0)


def test_uninstall_dissolves_undersized_group_and_preserves_raw(ready_db):
    before = rows('SELECT * FROM transactions_raw ORDER BY id')
    group = result(APPLY, {'op': 'create_group', 'name': '虚构组', 'items': ['tx:1', 'tx:3']})['created']['group_id']
    assert rows('SELECT COUNT(*) FROM ledger2_tx WHERE net_group_id=?', (group,)) == [(2,)]
    receipt = result(APPLY, {'op': 'uninstall', 'tx_ids': [1]})
    assert receipt['tx_count'] == 1
    assert rows('SELECT * FROM ledger2_net_group WHERE id=?', (group,)) == []
    assert rows('SELECT tx_id,net_group_id FROM ledger2_tx WHERE tx_id IN (1,3)') == [(3, None)]
    assert rows('SELECT * FROM transactions_raw ORDER BY id') == before


def test_call_key_and_expected_stack_top_prevent_duplicate_or_wrong_operations(ready_db):
    args = {'op': 'move', 'items': ['tx:1'], 'target': 'root'}
    before = ledger_rows()
    moved = result(APPLY, args, key='run-synthetic:10:0')
    after = ledger_rows()
    assert after != before and moved == result(APPLY, args, key='run-synthetic:10:0')
    assert error(APPLY, {**args, 'items': ['tx:3']}, key='run-synthetic:10:0') == 'CALL_MISMATCH'
    assert error(APPLY, {'op': 'undo', 'expected_seq': moved['seq'] + 1}) == 'HISTORY_CHANGED'
    assert ledger_rows() == after
    assert result(APPLY, {'op': 'undo', 'expected_seq': moved['seq']})['seq'] == moved['seq']
    assert ledger_rows() == before
    assert error(APPLY, {'op': 'redo', 'expected_seq': moved['seq'] + 1}) == 'HISTORY_CHANGED'
    assert result(APPLY, {'op': 'redo', 'expected_seq': moved['seq']})['seq'] == moved['seq']
    assert ledger_rows() == after
    assert rows('SELECT seq,state FROM ledger2_op_log') == [(moved['seq'], 'applied')]
    assert rows('SELECT COUNT(*) FROM agentcore_ledger_receipt') == [(1,)]


def test_ledger_does_not_commit_if_receipt_cannot_be_written(raw_db):
    with closing(db.get_conn()) as conn, conn:
        conn.execute("CREATE TRIGGER reject_test_receipt BEFORE INSERT ON agentcore_ledger_receipt "
                     "BEGIN SELECT RAISE(ABORT,'synthetic receipt failure'); END")
    args = {'op': 'init', 'tx_ids': [1], 'new_node': {'name': '不能半提交'}}
    baseline = ledger_rows()
    assert error(APPLY, args, key='run-synthetic:20:0') == 'TOOL_CRASH'
    assert ledger_rows() == baseline
    assert rows('SELECT * FROM ledger2_op_log') == rows('SELECT * FROM agentcore_ledger_receipt') == []


def test_concurrent_commit_is_one_batch_even_after_file_cleanup(staged):
    stage_id = staged['stage_id']
    start = Barrier(2)

    def attempt(_):
        start.wait(timeout=5)
        return result(COMMIT, {'stage_id': stage_id})

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = list(pool.map(attempt, range(2)))
    assert a == b == result(COMMIT, {'stage_id': stage_id})
    receipt = a['receipt']
    assert (receipt['inserted'], receipt['skipped'], receipt['failed']) == (3, 0, 0)
    assert import_tool.stage_status(stage_id) == {k: v for k, v in a.items() if k != 'ok'}
    assert not import_tool._row_path(stage_id).exists()
    assert rows('SELECT batch_id,inserted FROM import_batches') == [(receipt['batch_id'], 3)]
    assert rows('SELECT DISTINCT import_batch FROM transactions_raw') == [(receipt['batch_id'],)]
    assert rows('SELECT * FROM ledger2_tx') == []


def test_import_rolls_back_rows_and_keeps_retryable_stage(staged, monkeypatch):
    original = Row.to_sql_tuple
    seen = 0

    def fail_on_second(self, batch_id):
        nonlocal seen
        seen += 1
        if seen == 2:
            raise ValueError('synthetic insert failure')
        return original(self, batch_id)

    stage_id = staged['stage_id']
    with monkeypatch.context() as patch:
        patch.setattr(Row, 'to_sql_tuple', fail_on_second)
        assert error(COMMIT, {'stage_id': stage_id}) == 'IMPORT_FAILED'
    assert rows('SELECT * FROM transactions_raw') == rows('SELECT * FROM import_batches') == []
    assert import_tool.stage_status(stage_id)['state'] == 'staged'
    assert result(COMMIT, {'stage_id': stage_id})['receipt']['inserted'] == 3


def test_old_import_card_is_manual_and_reads_existing_receipt(staged):
    stage_id = staged['stage_id']
    card = import_tool.prepare(stage_id, session_id=None)
    assert card['state'] == 'pending' and rows('SELECT * FROM import_batches') == []
    executed = result(COMMIT, {'stage_id': stage_id})
    assert rows('SELECT state FROM agentcore_action WHERE id=?', (card['action_id'],)) == [('pending',)]
    decided = import_tool.confirm(card['action_id'])
    assert decided['state'] == 'confirmed' and decided['receipt'] == executed['receipt']
    assert rows('SELECT COUNT(*) FROM import_batches') == [(1,)]


def test_old_ledger_card_does_not_write_until_confirmed_and_is_single_use(raw_db):
    card = actions.propose(None, {'type': 'init', 'tx_ids': [1], 'style': 'platform'})
    assert card['state'] == 'pending' and card['preview']['counts']['created'] >= 2
    assert rows('SELECT * FROM ledger2_tx') == []

    def attempt(_):
        try:
            return actions.confirm(card['action_id'])['state']
        except actions.ActionError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, range(2))) == ['ACTION_NOT_PENDING', 'confirmed']
    assert rows('SELECT COUNT(*) FROM ledger2_op_log') == [(1,)]


def test_stale_manual_card_is_rejected_after_direct_write(ready_db):
    args = {'items': ['tx:1'], 'target': 'root'}
    old = actions.propose(None, {'type': 'move', **args})
    result(APPLY, {'op': 'move', **args})
    with pytest.raises(actions.ActionError) as exc:
        actions.confirm(old['action_id'])
    assert exc.value.code == 'STALE_ACTION'
    assert rows('SELECT state FROM agentcore_action WHERE id=?', (old['action_id'],)) == [('pending',)]


def test_classification_examples_are_per_tx_still_current_and_user_confirmed(ready_db):
    actions.propose(None, {'type': 'move', 'items': ['tx:1'], 'target': 'root'})
    result(APPLY, {'op': 'move', 'items': ['tx:2'], 'target': 'root'})
    assert result(READ, {'mode': 'classification_examples'})['examples'] == []
    card = actions.propose(None, {'type': 'create_node', 'name': '示例分类', 'items': ['tx:1', 'tx:3']})
    actions.confirm(card['action_id'])
    examples = result(READ, {'mode': 'classification_examples'})['examples']
    assert {p['tx_id'] for p in examples} == {1, 3}
    assert all(p['source'] == 'user_confirmed' and p['node_name'] == '示例分类' for p in examples)
    result(APPLY, {'op': 'uninstall', 'tx_ids': [1]})
    assert [p['tx_id'] for p in result(READ, {'mode': 'classification_examples'})['examples']] == [3]


def test_mcp_sdk_and_web_registry_share_business_schema_and_results(ready_db, staged):
    from mcp.shared.memory import create_connected_server_and_client_session

    from backend.mcp import server as mcp

    async def check():
        async with create_connected_server_and_client_session(mcp.server) as client:
            resources = (await client.list_tools()).tools
            web = registry.openai_tools(scope='business')
            assert [(t.name, t.description, t.inputSchema) for t in resources] == [
                (t['function']['name'], t['function']['description'], t['function']['parameters']) for t in web
            ]
            assert len(resources) == 6
            assert not {'finance_ledger_propose', 'finance_import_prepare', 'todo_write', 'read_memory'} & {
                t.name for t in resources}
            skill = await client.call_tool('read_skill', {'skill_id': 'ledger-classify'})
            assert not skill.isError and '<skill_content' in mcp._payload(skill)['content']
            args = {'stage_id': staged['stage_id']}
            imported = await client.call_tool(COMMIT, args)
            assert not imported.isError and mcp._payload(imported) == result(COMMIT, args)
            action = {'op': 'move', 'items': ['tx:1'], 'target': 'root'}
            applied = await client.call_tool(APPLY, action)
            assert not applied.isError and mcp._payload(applied)['state'] == 'executed'
            assert rows('SELECT node_id FROM ledger2_tx WHERE tx_id=1') == [('root',)]
            args = {'mode': 'summary'}
            read = await client.call_tool(READ, args)
            assert not read.isError and mcp._payload(read) == result(READ, args)
            assert rows('SELECT COUNT(*) FROM agentcore_action') == [(0,)]
    asyncio.run(check())
