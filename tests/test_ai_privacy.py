from __future__ import annotations

import asyncio
import json
from contextlib import closing

import httpx

from agentcore_support import call_reply, tool
from backend import db
from backend.agentcore import adapter, sql_tool
from backend.ai import privacy, providers
from backend.ai.config import AIConfig
from ledger2_support import make_db
from test_agentcore_web import app, body, create, events, web


PRIVATE = '张小明'
CUSTOM = '同学A'
PLACE = '合成地点青竹楼'
BUSINESS = '南区食堂'
PRIVACY_URL = '/api/ai/settings/privacy'


def seed():
    with closing(make_db(db.DB_PATH, rows=[
        dict(id=1, tx_time='2026-01-05 10:00:00', direction='支出', amount=30,
             tx_type=f'聚餐 / {PRIVATE}', counterparty=PRIVATE, item=f'和{PRIVATE}聚餐',
             remark=f'地点：{PLACE}', raw={'交易对方': PRIVATE, '地点': PLACE, '对方账号': 'test-account'}),
        dict(id=2, tx_time='2026-01-05 12:00:00', direction='支出', amount=16,
             tx_type='餐饮', counterparty=BUSINESS, item='午餐'),
        dict(id=3, tx_time='2026-01-05 16:00:00', direction='支出', amount=10,
             tx_type='购物', counterparty='李家网店', item='文具'),
    ])):
        pass


def test_auto_screening_and_aliases_in_query_snapshot(agentcore_ready):
    seed()
    [entry] = privacy.settings()
    assert (entry['original'], entry['count'], entry['mode']) == (PRIVATE, 1, 'auto')
    assert entry['alias'].startswith('联系人-')
    db.init_db()
    assert privacy.settings() == [entry]
    assert privacy.current().text(f'{PRIVATE}在{BUSINESS}') == f"{entry['alias']}在{BUSINESS}"

    privacy.save_alias(PRIVATE, CUSTOM)
    privacy.save_alias(PLACE, '聚餐地点')
    data = sql_tool.query(
        "SELECT counterparty, amount, item, remark, json_extract(raw_json, '$.交易对方'), "
        "json_extract(raw_json, '$.对方账号'), hex(counterparty) FROM transactions_raw WHERE id=1"
    )
    assert data['rows'] == [[CUSTOM, 30, f'和{CUSTOM}聚餐', '地点：聚餐地点', CUSTOM, '[隐私信息]',
                             CUSTOM.encode().hex().upper()]]
    assert sql_tool.query(f"SELECT tx_id FROM agentcore_tx WHERE counterparty='{CUSTOM}'")['rows'] == [[1]]
    policy = privacy.current()
    assert policy.text(entry['alias']) == CUSTOM
    assert policy.ledger_refs({'target': f'cat:聚餐 / {CUSTOM}', 'name': CUSTOM}) == {
        'target': f'cat:聚餐 / {PRIVATE}', 'name': CUSTOM,
    }
    assert policy.redact(json.dumps({'text': PRIVATE})) == json.dumps({'text': CUSTOM}, ensure_ascii=False)

    privacy.save_alias(PRIVATE, '')
    assert privacy.current().text(PRIVATE) == PRIVATE
    privacy.save_alias(PRIVATE, None)
    assert privacy.current().text(PRIVATE) == entry['alias']


def test_settings_keep_local_views_original_and_mask_atlas(app, monkeypatch):
    async def fake_model(_config, **request):
        text = json.dumps(request['messages'], ensure_ascii=False)
        assert PRIVATE not in text and PLACE not in text
        assert CUSTOM in text
        if request['messages'][-1]['role'] == 'tool':
            assert '聚餐地点' in text
            return adapter.ModelReply(text=f'{PRIVATE}在{PLACE}消费了 30 元。')
        return call_reply(tool('privacy-read', 'finance_ledger_read', {'mode': 'tx', 'ref': 'tx:1'}))

    monkeypatch.setattr(adapter, 'complete', fake_model)

    async def run():
        async with web(app) as client:
            seed()
            auto = (await body(client.get(PRIVACY_URL)))['entries']
            assert [entry['original'] for entry in auto] == [PRIVATE]
            for original, alias in [(PRIVATE, CUSTOM), (PLACE, '聚餐地点')]:
                await body(client.put(PRIVACY_URL, json={'original': original, 'alias': alias}))
            invalid = await client.put(PRIVACY_URL, json={'original': '李同学', 'alias': CUSTOM})
            assert invalid.status_code == 400
            for route in ['/api/tx/1', '/api/report/transactions?type=expense&period=month&value=2026-01',
                          '/api/ledger2/tree']:
                data = await body(client.get(route))
                assert PRIVATE in json.dumps(data, ensure_ascii=False)
                assert CUSTOM not in json.dumps(data, ensure_ascii=False)
            url = await create(client, '脱敏验证')
            turn = await body(client.post(f'{url}/turns', json={'text': f'查{PRIVATE}在{PLACE}的交易'}), 202)
            frames = await events(client, f"{url}/turns/{turn['id']}")
            items = [frame.get('item', {}) for frame in frames]
            assistant = [item for item in items if item.get('type') == 'assistant']
            assert assistant[-1]['text'] == f'{CUSTOM}在聚餐地点消费了 30 元。'
            tool_items = [item for item in items if item.get('type') == 'tool']
            assert not tool_items[0]['is_error']
            assert PRIVATE not in json.dumps(tool_items, ensure_ascii=False)

    asyncio.run(run())


def test_provider_masks_unimported_counterparties(agentcore_ready, monkeypatch):
    data = {'rows': [{'counterparty': PRIVATE, 'remark': f'和{PRIVATE}聚餐', 'row_index': 0},
                     {'counterparty': BUSINESS, 'row_index': 1}]}

    async def post(_self, url, **kwargs):
        messages = kwargs['json']['messages']
        text = messages[-1]['content']
        assert PRIVATE not in text and BUSINESS in text
        assert '联系人-' in text
        return httpx.Response(200, request=httpx.Request('POST', url), json={
            'choices': [{'message': {'content': json.dumps({'assistant_message': f'{PRIVATE}聚餐'})}}],
        })

    monkeypatch.setattr(httpx.AsyncClient, 'post', post)
    config = AIConfig(provider='openai_compatible', base_url='https://test.invalid/v1',
                      model='synthetic', api_key='synthetic')
    result = asyncio.run(providers.complete_json(config, system_prompt='按行号分类', user_payload=data))
    assert PRIVATE not in result['assistant_message']
    assert data['rows'][0]['counterparty'] == PRIVATE
