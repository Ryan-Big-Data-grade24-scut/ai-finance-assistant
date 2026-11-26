"""Current-period report letters, stored in the selected ledger."""
import json
from contextlib import closing
from datetime import date

from backend import db
from backend.ledger2.tx import atomic


def current_value(period: str, today: date) -> str:
    if period == 'week':
        year, week, _ = today.isocalendar()
        return f'{year}-W{week:02}'
    if period == 'month':
        return today.isoformat()[:7]
    return str(today.year)


def read(period: str, value: str) -> dict:
    if value != current_value(period, date.today()):
        return {'current': False, 'brief': None}
    with closing(db.get_conn()) as conn:
        row = conn.execute('SELECT body FROM report_briefs WHERE period=? AND value=?',
                           (period, value)).fetchone()
    return {'current': True, 'brief': json.loads(row[0]) if row else None}


class BriefError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def write(args: dict, call_key: str | None) -> dict:
    period, value = args['period'], args['value']
    with closing(db.get_conn()) as conn, atomic(conn):
        if call_key:
            previous = conn.execute('SELECT args_json,result_json FROM agentcore_ledger_receipt WHERE call_key=?',
                                    (call_key,)).fetchone()
            if previous:
                if json.loads(previous['args_json']) != args:
                    raise BriefError('CALL_MISMATCH', '调用参数与已保存的简报回执不一致。')
                return json.loads(previous['result_json'])
        today = date.today()
        if value != current_value(period, today):
            raise BriefError('BRIEF_PERIOD', f'只能写入包含今天的周期，当前 {period} 为 {current_value(period, today)}。')
        income, fixed, flex = (args[key] for key in ('income_cents', 'obligatory_cents', 'flexible_cents'))
        expense = fixed + flex if fixed is not None and flex is not None else None
        body = {**args, 'as_of': today.isoformat(), 'expense_cents': expense,
                'free_cents': income - fixed if income is not None and fixed is not None else None,
                'surplus_cents': income - expense if income is not None and expense is not None else None}
        conn.execute('INSERT INTO report_briefs(period,value,body) VALUES(?,?,?) '
                     'ON CONFLICT(period,value) DO UPDATE SET body=excluded.body',
                     (period, value, json.dumps(body, ensure_ascii=False)))
        result = {'state': 'written', 'period': period, 'value': value, 'as_of': body['as_of']}
        if call_key:
            conn.execute('INSERT INTO agentcore_ledger_receipt VALUES(?,?,?)',
                         (call_key, json.dumps(args, sort_keys=True), json.dumps(result)))
    return result
