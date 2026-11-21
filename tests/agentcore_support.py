"""合成账单与脚本模型；只从模型收到的历史中取得业务引用。"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from backend import db, importer
from backend.agentcore import adapter
from backend.ai.config import AIConfig

STUB_CONFIG = AIConfig(
    provider='openai_compatible', base_url='http://stub.invalid/v1', model='agentcore-scripted',
    api_key='not-a-real-key', allow_external_processing=True,
)

IMPORT_GOAL = (
    '导入右侧已解析的合成账单，只把这批实际新增的交易初始化到一个新节点「示例报销」，'
    '再把这两笔建成净额组「示例净额组」。重读核对，告诉我实际新增笔数和净额。'
)
FOLLOW_UP = '撤掉刚才的组，再告诉我净额。'
NODE_NAME = '示例报销'
GROUP_NAME = '示例净额组'
DEMO_FILENAME = 'agentcore-demo-alipay.csv'

_HEADER = ('支付宝交易记录明细查询\n'
           '收/支,交易时间,交易分类,交易对方,商品说明,金额,收/付款方式,交易状态,交易订单号,商家订单号,备注\n')
_DEMO_ROWS = (
    '支出,2026-03-02 12:00:00,餐饮,虚构对方甲,示例餐费,32.00,模拟余额,交易成功,ac-demo-1,,仅供测试',
    '收入,2026-03-03 12:00:00,其他收入,虚构对方乙,示例报销,12.00,模拟余额,交易成功,ac-demo-2,,仅供测试',
    '支出,2026-03-04 12:00:00,交通,虚构对方丙,示例车票,8.00,模拟余额,交易成功,ac-demo-3,,预置重复项',
)
_UNRELATED = '支出,2026-03-01 12:00:00,购物,虚构对方丁,示例纸笔,9.00,模拟余额,交易成功,ac-demo-4,,非本批'
DEMO_CSV = (_HEADER + '\n'.join(_DEMO_ROWS) + '\n').encode('utf-8-sig')


def seed_demo_history(directory: Path) -> str:
    """先导入一个重复项和一个无关项，两笔都保持未初始化。"""
    assert db.DB_PATH.parent == directory and db.DB_PATH.name == 'synthetic.db'
    path = directory / 'prior-alipay.csv'
    path.write_bytes((_HEADER + '\n'.join((_DEMO_ROWS[-1], _UNRELATED)) + '\n').encode('utf-8-sig'))
    result = importer.import_file(path)
    assert (result.inserted, result.skipped, result.failed) == (2, 0, 0)
    return result.batch_id


def install_config(monkeypatch, **overrides) -> AIConfig:
    from backend.agentcore import runtime

    cfg = replace(STUB_CONFIG, **overrides)
    monkeypatch.setattr(runtime.ai_config, 'get_secret_config', lambda: cfg)
    return cfg


def tool(call_id: str, name: str, args: dict) -> adapter.ToolCall:
    return adapter.ToolCall(call_id, name, json.dumps(args, ensure_ascii=False))


def call_reply(*calls: adapter.ToolCall, **kwargs) -> adapter.ModelReply:
    return adapter.ModelReply(tool_calls=list(calls), stop_reason='tool_calls', **kwargs)


def _do(key: str, tool_name: str, **args) -> adapter.ModelReply:
    return call_reply(tool(key, tool_name, args))


def _results(messages: list[dict]) -> dict:
    results = {}
    for message in messages:
        if message['role'] == 'tool':
            value = json.loads(message['content'])
            assert value['ok'], value
            results[message['tool_call_id']] = value
    return results


def _pending(*items: tuple[str, str]) -> list[dict]:
    return [{'content': text, 'status': status} for text, status in items]


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _import_chain(history: list[dict], results: dict) -> adapter.ModelReply:
    if 'plan' not in results:
        return _do('plan', 'todo_write', todos=_pending(
            ('核对已解析账单', 'in_progress'), ('导入并筛选实际新增', 'pending'),
            ('初始化、建净额组并复核', 'pending'),
        ))
    if 'skill-import' not in results:
        return _do('skill-import', 'read_skill', skill_id='finance-import')
    if 'stage' not in results:
        context, = [m['content'] for m in history if m['role'] == 'user' and '<turn_context>' in m['content']]
        line, = [line for line in context.splitlines() if line.startswith('本页附件曾解析成功')]
        stage_id = json.loads(line.split('：', 1)[1])['stage_id']
        return _do('stage', 'finance_import_read', mode='stage', stage_id=stage_id)
    if 'commit' not in results:
        return _do('commit', 'finance_import_commit', stage_id=results['stage']['stage']['stage_id'])
    receipt = results['commit']['receipt']
    if 'candidates' not in results:
        return _do('candidates', 'finance_query_transactions', sql=(
            'SELECT a.tx_id, a.category, a.direction, a.amount_cents FROM agentcore_tx a '
            'JOIN transactions_raw r ON r.id=a.tx_id WHERE a.initialized=0 AND r.import_batch='
            + _quote(receipt['batch_id']) + ' ORDER BY a.tx_id'
        ))
    tx_ids = [row[0] for row in results['candidates']['rows']]
    assert len(tx_ids) == receipt['inserted'] == 2
    if 'skill-init' not in results:
        return _do('skill-init', 'read_skill', skill_id='ledger-initialize-uninstall')
    if 'skill-classify' not in results:
        return _do('skill-classify', 'read_skill', skill_id='ledger-classify')
    if 'examples' not in results:
        return _do('examples', 'finance_ledger_read', mode='classification_examples')
    if 'init' not in results:
        return _do('init', 'finance_ledger_apply', op='init', tx_ids=tx_ids, new_node={'name': NODE_NAME})
    node_id = results['init']['created']['nodes']['new']
    if 'node' not in results:
        return _do('node', 'finance_ledger_read', mode='node', ref=node_id)
    if 'skill-select' not in results:
        return _do('skill-select', 'read_skill', skill_id='ledger-select-operate')
    if 'group' not in results:
        return _do('group', 'finance_ledger_apply', op='create_group', name=GROUP_NAME,
                   items=[item['ref'] for item in results['node']['items']])
    if 'group-read' not in results:
        return _do('group-read', 'finance_ledger_read', mode='node', ref='g:' + results['group']['created']['group_id'])
    if 'root' not in results:
        return _do('root', 'finance_ledger_read', mode='summary')
    if 'finished' not in results:
        return _do('finished', 'todo_write', todos=_pending(
            ('核对已解析账单', 'completed'), ('导入并筛选实际新增', 'completed'),
            ('初始化、建净额组并复核', 'completed'),
        ))
    group = results['group-read']['node']
    return adapter.ModelReply(text=f"实际新增 {receipt['inserted']} 笔，净支出 ¥{(group['e'] - group['i']) / 100:.2f}。")


async def demo_model(_config, *, system, messages, tools, cache_key=None):
    """固定任务的协议替身，不声称能理解或规划任意任务。续接轮不靠进程私藏的组 ID。"""
    index, goal = next((i, m['content']) for i, m in reversed(list(enumerate(messages)))
                       if m['role'] == 'user' and m['content'] in {IMPORT_GOAL, FOLLOW_UP})
    current = _results(messages[index + 1:])
    if goal == IMPORT_GOAL:
        return _import_chain(messages[:index], current)

    previous = _results(messages[:index])
    if 'dissolve' not in current:
        return _do('dissolve', 'finance_ledger_apply', op='dissolve_group',
                   group_id=previous['group']['created']['group_id'])
    if 'after' not in current:
        return _do('after', 'finance_ledger_read', mode='node', ref=previous['init']['created']['nodes']['new'])
    node = current['after']['node']
    return adapter.ModelReply(text=f"已解散组，净支出 ¥{(node['e'] - node['i']) / 100:.2f}。")
