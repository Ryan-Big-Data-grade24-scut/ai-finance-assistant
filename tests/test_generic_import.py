from contextlib import closing
from io import BytesIO

import openpyxl
import pytest
from fastapi.testclient import TestClient

from backend import db, libraries
from backend.ai.config import AIConfig
from backend.importer import generic
from backend.main import app


@pytest.mark.parametrize('extension', ['csv', 'xlsx'])
def test_ai_maps_only_headers_and_commit_reuses_preview(agentcore_paths, monkeypatch, extension):
    monkeypatch.setattr(libraries, '_registry_path', None)
    settings = AIConfig('openai_compatible', 'https://example.test/v1', 'mock', 'test-key',
                        allow_external_processing=True)
    monkeypatch.setattr(generic.config, 'get_secret_config', lambda: settings)
    calls = []

    async def model(config, *, system_prompt, user_payload, timeout):
        calls.append(user_payload)
        return {'mode': 'column', 'columns': {'tx_time': 0, 'amount': 1, 'direction': 2, 'counterparty': 3}}

    monkeypatch.setattr(generic.providers, 'complete_json', model)
    headers = ('业务发生于', '流水实收付', '钱款流向', '来往主体')
    row = ('2026-11-01', '82.50', '支出', '模拟交易对方')
    if extension == 'xlsx':
        workbook = openpyxl.Workbook()
        workbook.active.append(['模拟账单'])
        workbook.active.append(headers)
        workbook.active.append(row)
        stream = BytesIO()
        workbook.save(stream)
        workbook.close()
        content = stream.getvalue()
    else:
        content = (','.join(headers) + '\n' + ','.join(row) + '\n').encode()
    file = {'file': (f'其他平台.{extension}', content)}
    with TestClient(app) as client:
        preview = client.post('/api/import/preview', files=file)
        assert preview.status_code == 200, preview.text
        token = preview.json()['mapping_token']
        assert calls[0]['headers'] == headers
        assert '模拟交易对方' not in str(calls)

        modified = {'file': (f'另一个平台.{extension}', content)}
        rejected = client.post('/api/import/commit', files=modified, data={'mapping_token': token})
        assert rejected.status_code == 400
        result = client.post('/api/import/commit', files=file, data={'mapping_token': token})
        assert result.status_code == 200, result.text
        assert result.json()['inserted'] == 1
        repeat = client.post('/api/import/commit', files=file, data={'mapping_token': token})
        assert repeat.json()['skipped'] == 1
        assert len(calls) == 1
        with closing(db.get_conn()) as conn:
            row = conn.execute('SELECT source, direction, tx_time, amount, counterparty FROM transactions_raw').fetchone()
        assert tuple(row) == ('其他平台', '支出', '2026-11-01 00:00:00', 82.5, '模拟交易对方')


def test_snapshot_is_not_a_transaction_bill(agentcore_paths, monkeypatch):
    monkeypatch.setattr(libraries, '_registry_path', None)
    file = {'file': ('资产快照.csv', '日期,账户名称,市值,成本,浮动盈亏\n2026-11-01,测试产品,123,100,23\n'.encode())}
    with TestClient(app) as client:
        preview = client.post('/api/import/preview', files=file)
    assert preview.status_code == 400
    assert '资产快照' in preview.json()['detail']
