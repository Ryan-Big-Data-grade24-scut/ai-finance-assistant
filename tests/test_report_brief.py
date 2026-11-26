from datetime import date

from fastapi.testclient import TestClient

from backend import db
from backend.agentcore import registry
from backend.main import app
from backend.reports.brief import current_value


def test_report_letter_lifecycle(agentcore_ready, monkeypatch, tmp_path):
    client = TestClient(app)
    tool = 'finance_report_brief_write'
    ctx = registry.ToolContext(call_key='test-brief')
    month = current_value('month', date.today())
    query = {'period': 'month', 'value': month}
    read = lambda: client.get('/api/report/brief', params=query).json()
    assert read() == {'brief': None, 'current': True}
    args = {**query, 'summary': '基于固定缴费估算', 'income_cents': 22001,
            'obligatory_cents': 20000, 'flexible_cents': 4000,
            'flags': ['核对连续两笔相同扣费'], 'advice': ['查看续费日期']}
    _, failed = registry.execute(tool, args, ctx, scope='business')
    assert not failed
    assert registry.recover(tool, args, ctx)['value'] == month
    first = read()['brief']
    assert (first['expense_cents'], first['free_cents'], first['surplus_cents']) == (24000, 2001, -1999)
    assert first['flags'] == args['flags'] and first['advice'] == args['advice']
    for edits in (
        {'summary': '长' * 46},
        {'summary': '收支偏紧\n'},
        {'flags': ['核对订阅扣款'] * 3},
        {'flags': ['长' * 33]},
        {'advice': ['核对续费日', '暂停非必要订阅']},
        {'advice': ['长' * 33]},
    ):
        assert registry.execute(tool, {**args, **edits}, registry.ToolContext())[1]
    assert read()['brief'] == first
    assert not registry.execute(tool, {**args, 'income_cents': None}, registry.ToolContext())[1]
    assert not registry.execute(tool, args, ctx)[1]  # replaying an old call cannot overwrite a later edit
    assert read()['brief']['free_cents'] is None
    for period in ['week', 'year']:
        q = {'period': period, 'value': current_value(period, date.today())}
        assert client.get('/api/report/brief', params=q).json()['brief'] is None
        assert not registry.execute(tool, {**args, **q}, registry.ToolContext())[1]
        assert client.get('/api/report/brief', params=q).json()['brief']['period'] == period
    for historical in ['2001-01', '2099-12']:
        q = {**query, 'value': historical}
        assert client.get('/api/report/brief', params=q).json() == {'current': False, 'brief': None}
        assert registry.execute(tool, {**args, **q}, registry.ToolContext())[1]
    assert registry.execute(tool, {**args, 'income_cents': -1}, registry.ToolContext())[1]
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'another.db')
    db.init_db()
    assert read()['brief'] is None


def test_iso_week_spans_two_calendar_years():
    assert current_value('week', date(2027, 1, 1)) == '2026-W53'
