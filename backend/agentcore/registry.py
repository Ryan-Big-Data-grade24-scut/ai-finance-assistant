"""Web 和 V2 MCP 共用的工具契约：什么时候用、为什么、参数与边界。"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from jsonschema import Draft202012Validator, validators

from backend.ai import privacy
from backend.reports import brief
from backend.ledger2.errors import LedgerError

from . import import_tool, ledger_tools, memory, skills, sql_tool, truncate
from .ledger_tools import ToolInputError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolContext:
    session_id: str | None = None
    # 运行器根据轮次和调用位置生成，不能由模型提供。
    call_key: str | None = None
    run_id: str | None = None


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    schema: dict
    handler: Callable[[dict, ToolContext], dict]
    scope: str = 'business'
    # 同一批只读调用可以真正并行；任何写调用仍按模型给出的顺序执行。
    read_only: bool = False


def _object(properties: dict, *required: str) -> dict:
    return {'type': 'object', 'properties': properties, 'required': list(required), 'additionalProperties': False}


def _string(description: str = '', *, nonempty: bool = False) -> dict:
    return {'type': 'string', 'description': description, **({'minLength': 1} if nonempty else {})}


def _array(items: dict, description: str = '', *, minimum: int = 0) -> dict:
    return {'type': 'array', 'items': items, 'description': description, 'minItems': minimum}


_TEXT = _string()
_ID = _string(nonempty=True)
_TX_ID = {'type': 'integer', 'minimum': 1}
_TX_REF = {'anyOf': [_TX_ID, _ID]}
_PAGING = {
    'offset': {'type': 'integer', 'minimum': 0, 'description': '首屏省略；下一页传回执的 next_offset。'},
    'limit': {'type': 'integer', 'minimum': 1, 'description': '最多返回的完整条目数，默认 25。'},
}
_DATE = {'type': 'string', 'format': 'date', 'description': 'YYYY-MM-DD；起止同时给。'}

_LEDGER_READ = _object({
    'mode': {'type': 'string', 'enum': ['summary', 'tree', 'node', 'tx', 'classification_examples'],
             'description': 'summary=总览和撤销栈；tree=根页；node=指定节点或净额组；tx=单笔；classification_examples=仍有效的旧手工确认范例。'},
    'ref': _string('node/tx 模式的引用：节点 id、g:组id、tx:12（原始交易）、txm:12（镜像交易）。', nonempty=True),
    'date_from': _DATE, 'date_to': _DATE, **_PAGING,
}, 'mode')

_INIT_PLAN = _object({
    'nodes': _array(_object({'ref': _ID, 'name': _ID, 'parent': _ID, 'id': _ID, 'note': _TEXT}, 'ref', 'name'),
                    '新节点。parent 是已有节点 id 或前面定义的 ref，省略为根；id 不给由引擎生成。'),
    'place': _array(_object({'node': _ID, 'txs': _array(_TX_ID, minimum=1)}, 'node', 'txs'),
                    'node 是已有 id 或 nodes 中的 ref；未指定归属的交易放根。'),
    'splits': _array(_object({'tx': _TX_ID, 'node': _ID}, 'tx'), '生成镜像；node 不给时镜像交易与原始交易同节点。'),
    'groups': _array(_object({'name': _ID, 'txs': _array(_TX_REF, minimum=2), 'note': _TEXT}, 'name', 'txs'),
                     '净额组；txs 是原始交易或镜像交易的 id/引用，成员必须同父。'),
    'meta': {'type': 'object', 'patternProperties': {r'^[1-9][0-9]*$': _object({'title': _TEXT, 'note': _TEXT})},
             'additionalProperties': False, 'description': '按交易 id 字符串索引的人工标题和备注。'},
})

_LEDGER_APPLY = _object({
    'op': {'type': 'string', 'enum': sorted(ledger_tools._OPS), 'description': '要执行的 V2 操作。'},
    'items': _array(_ID, '选择引用：节点 id、tx:12（原始交易）、txm:12（镜像交易）、g:组id。新建分组、移动、加入净额组、新建净额组、解散、生成镜像、取消镜像用。', minimum=1),
    'target': _string('move 或 init 的目标节点 id。', nonempty=True),
    'new_node': {**_object({'name': _ID, 'parent': _ID}, 'name'),
                 'description': 'init 时新建节点再放入。parent 省略时挂根。'},
    'plan': {**_INIT_PLAN, 'description': '自定义 init，依次建节点、放置、生成镜像、建净额组。{} 表示放到根。'},
    'tx_ids': _array(_TX_ID, 'init/uninstall 的明确交易 id 列表。', minimum=1),
    'all_uninitialized': {'type': 'boolean', 'description': 'init 时明确选择全部未初始化交易；与 tx_ids 互斥。'},
    'style': {'type': 'string', 'enum': ['platform'], 'description': 'init 按账单平台给出的 category 字段建节点并放入（如餐饮/交通/购物/其他收入），不是按微信/支付宝/银行等 source 分组；按来源分组要用 plan 自己建节点。'},
    'group_id': _string('join/add_member/remove_member/rename_group/dissolve_group 的组 id（不带 g:）。', nonempty=True),
    'tx': {**_TX_REF, 'description': 'add_member/remove_member 的交易 id、tx:N 或 txm:N。'},
    'name': _string('创建/改名时的名称。'),
    'note': _string('备注；更新时给空串可清除。'),
    'item': _string('update 的单个对象引用。', nonempty=True),
    'node_id': _string('dissolve_node/rename_node/update_node 的节点 id。', nonempty=True),
    'tx_id': {**_TX_ID, 'description': '生成镜像/取消镜像的单笔选择（与 items 二选一），或 update_manual 的交易 id。'},
    'title': _string('update_manual 的人工标题。'),
    'expected_seq': {'type': 'integer', 'minimum': 1, 'description': 'undo/redo 可指定预期栈顶序号；先读 summary.status.history。'},
}, 'op')


TOOLS: dict[str, ToolSpec] = {}
_Validator = validators.extend(Draft202012Validator, type_checker=Draft202012Validator.TYPE_CHECKER.redefine(
    'integer', lambda _checker, value: type(value) is int,
))
_VALIDATORS: dict[str, _Validator] = {}


def _register(spec: ToolSpec) -> None:
    TOOLS[spec.name] = spec
    _VALIDATORS[spec.name] = _Validator(spec.schema, format_checker=Draft202012Validator.FORMAT_CHECKER)


def _read_skill(args: dict, _ctx: ToolContext) -> dict:
    try:
        return skills.load(args['skill_id'])
    except KeyError as exc:
        raise ToolInputError('SKILL_NOT_FOUND', str(exc)) from exc


def _read_memory(args: dict, _ctx: ToolContext) -> dict:
    try:
        return {'slug': args['slug'], 'content': memory.read_entry(args['slug'])}
    except KeyError as exc:
        raise ToolInputError('MEMORY_NOT_FOUND', str(exc)) from exc


_register(ToolSpec(
    name='finance_query_transactions',
    description=(
        '用一条只读 SQLite SELECT/WITH 查流水字段、筛选候选、聚合或对比；同一 SQL 面覆盖多种问法。'
        'transactions_raw 是流水凭证的查询快照（包含逻辑删除前留下的记录）：'
        'id, source, platform_id, tx_time, direction, amount(元), pay_method, counterparty, item, tx_type, '
        'status, tx_no, merchant_no, remark, date, month, year, weekday, hour, dedupe_key, raw_json, imported_at, import_batch。'
        'agentcore_tx 是排除逻辑删除的分析口径：tx_id, date, month, direction(收入/支出/中性), amount_cents(整数分), '
        'source, platform_id, category(人工分类优先，再平台原分类，最后其他), item, counterparty, status, initialized(1=已进 V2)。'
        '按 id=tx_id 可取凭证详情。文本可能已按隐私设置替换为别名；请用可见别名查询。'
        '只能读这两张流水快照表；30 秒、500 行、60KB，超出用聚合或排序分页。'
        'coverage 是全集日期覆盖，不是 WHERE 命中期；row_count 是结果行数。问 V2 净额用 finance_ledger_read，不能拿流水求和替代。'
    ),
    schema=_object({'sql': _string('一条 SQLite SELECT/WITH。', nonempty=True)}, 'sql'),
    handler=lambda args, ctx: sql_tool.query(args['sql']), read_only=True,
))

_register(ToolSpec(
    name='finance_ledger_read',
    description=(
        '读 V2 树、金额、归属、人工字段和撤销历史。引擎先按范围投影、处理净额组和镜像交易，'
        '所以不能把原始流水 SUM 当账本。e/i 是整数分；树/节点/组用 next_offset 逐页读直属子项，'
        '每页金额仍是整个投影的引擎结果。用 ref 继续读子树、组或单笔（也能查组中单笔）。'
        'classification_examples 只来自仍与目标一致的旧手工确认，不把当前归属或助手直写冒充用户确认。'
    ),
    schema=_LEDGER_READ,
    handler=lambda args, ctx: ledger_tools.read(args), read_only=True,
))

_register(ToolSpec(
    name='finance_ledger_apply',
    description=(
        '直接执行 V2 账本操作，不生成待确认卡。明确对象和目标后调用，下一步用 state=executed 的真实回执，'
        'created 返回实际新对象 id，seq 是操作日志序号。复用页面同一引擎：净额组同父、至少两成员；'
        '移动、加入、新建会折叠被覆盖的选择，生成镜像（split）/取消镜像（unsplit）会展开；'
        'dissolve 只解散明确选中的分组或净额组，不展开未选中的子节点（子节点上移保留）；组内成员有整组移动等规则。'
        'init 的 style/target/new_node/plan 四选一，按指定归属一次放置比先全量再移动更直接。'
        '明确全量用 all_uninitialized=true，空数组不是全部。uninstall 只撤 V2 归属和人工字段，原始流水不动。'
        '连续操作根据上步回执和重读决定，不猜 id；undo/redo 是全账本栈，带 expected_seq 可确保目标正是指定那一步。'
    ),
    schema=_LEDGER_APPLY,
    handler=lambda args, ctx: ledger_tools.apply(args, call_key=ctx.call_key, run_id=ctx.run_id),
))

_register(ToolSpec(
    name='finance_import_read',
    description=(
        '查看解析后的账单或批次。stage 按 stage_id 分页读归一化明细，已提交时返回历史回执；'
        'stages 列出可用暂存，batches 查入库历史。先读可辨认用户说的是哪个附件、有哪些行和解析警告。'
        '文件由用户在本地表单选好；这里不接收路径或解压密码。解析成功不等于已入库。'
    ),
    schema=_object({'mode': {'type': 'string', 'enum': ['stage', 'stages', 'batches']},
                    'stage_id': _string('stage 模式指定页面上传得到的 id。', nonempty=True), **_PAGING}, 'mode'),
    handler=lambda args, ctx: import_tool.read(args), read_only=True,
))

_register(ToolSpec(
    name='finance_import_commit',
    description=(
        '将用户选择的已解析暂存直接提交到原始流水。凭固定 stage_id 重试会返回同一批次的真实回执，'
        '不会重复导入；以 inserted/skipped/failed 和 date_from/date_to 报结果。'
        '入库不等于初始化 V2：若用户也要求分类入账，再用回执的 batch_id 查实际新增候选、调用账本工具并复核。'
    ),
    schema=_object({'stage_id': _string('页面上传返回的暂存 id。', nonempty=True)}, 'stage_id'),
    handler=lambda args, ctx: import_tool.commit(args['stage_id']),
))

_ESTIMATE = {'type': ['integer', 'null'], 'minimum': 0, 'maximum': 99999999999999,
             'description': '从今天起至本期末尚未发生的估算金额（整数分）。依据不足传 null，确认无此项才传 0。'}
_BRIEF_LINE = {'type': 'string', 'minLength': 1, 'pattern': r'^[^\r\n]*\S[^\r\n]*(?![\s\S])'}
_BRIEF_FIELDS = {
    'period': {'enum': ['week', 'month', 'year'], 'type': 'string'},
    'value': _string('本地当天所在的 ISO 周 YYYY-Www、月 YYYY-MM 或年 YYYY。', nonempty=True),
    'summary': {**_BRIEF_LINE, 'maxLength': 45,
                'description': '一句话评价本期财务状况，最多 45 字。只说最重要的判断，省去标题、分析过程和下方已有的金额。'},
    'income_cents': _ESTIMATE, 'obligatory_cents': _ESTIMATE, 'flexible_cents': _ESTIMATE,
    'flags': {'type': 'array', 'maxItems': 2, 'items': {**_BRIEF_LINE, 'maxLength': 32},
              'description': '最多 2 项，每项最多 32 字，只写支出对象和核对原因。使用可见别名；无已识别项传空数组。'},
    'advice': {'type': 'array', 'maxItems': 1, 'items': {**_BRIEF_LINE, 'maxLength': 32},
               'description': '一句具体可执行的建议，最多 32 字。依据不足时传空数组。'},
}
_register(ToolSpec(
    name='finance_report_brief_write',
    description='用户要求撰写或更新报告简报时，先完成分析，再整封替换指定周期的信纸。'
                '上屏只需一句财务评价、六项收支数字、最多两项待核对支出和一句建议。文本用纯文字，省去推导和重复金额。'
                '周、月、年分别写入。预计支出等于义务性加弹性支出，未来活钱等于预计收入减义务性支出；'
                '扣掉弹性支出后的结余另算。系统自动计算，保留缺口和负数。',
    schema=_object(_BRIEF_FIELDS, *_BRIEF_FIELDS),
    handler=lambda args, ctx: brief.write(args, ctx.call_key),
))


def _skill_description() -> str:
    lines = ['开始某类业务时载入对应 Skill，读清操作顺序和理由。可用：']
    for skill in skills.catalog():
        line = f'- {skill.name}：{skill.description}'
        if skill.when_to_use:
            line += f'（何时用：{skill.when_to_use}）'
        lines.append(line)
    lines.append('先载入再执行；正文作为工具结果进入对话。')
    return '\n'.join(lines)


def _skill_schema() -> dict:
    prop = {'type': 'string', 'description': '要载入的 Skill id。'}
    ids = [skill.name for skill in skills.catalog()]
    if ids:
        prop['enum'] = ids
    else:
        prop['minLength'] = 1
    return _object({'skill_id': prop}, 'skill_id')


_register(ToolSpec(
    name='read_skill',
    description=_skill_description(),
    schema=_skill_schema(),
    handler=_read_skill, scope='business', read_only=True,
))

_register(ToolSpec(
    name='read_memory',
    description='本轮任务引用了用户以前的偏好时，按 slug 读工作记忆的细节。只注入索引和必要条目，避免每轮加载全部。',
    schema=_object({'slug': _string('工作记忆索引里的 slug。', nonempty=True)}, 'slug'),
    handler=_read_memory, scope='harness', read_only=True,
))

_register(ToolSpec(
    name='todo_write',
    description=(
        '覆盖本会话当前任务清单。复杂工作开始时列出步骤，拿到实际回执时更新，保留未完成项和已完成的关键步骤；'
        '简单问答不需要。清单跨刷新和轮次，不等于执行；业务效果只由业务工具回执和重读证实。'
    ),
    schema=_object({'todos': _array(_object({
        'content': _string('简短的步骤。', nonempty=True),
        'status': {'type': 'string', 'enum': ['pending', 'in_progress', 'completed']},
    }, 'content', 'status'), '完整的新清单；空数组表示清空。')}, 'todos'),
    handler=lambda args, ctx: {'todos': args['todos']}, scope='harness', read_only=True,
))


def is_read_only(name: str) -> bool:
    spec = TOOLS.get(name)
    return bool(spec and spec.read_only)


def _select(scope: str) -> list[ToolSpec]:
    return [spec for spec in TOOLS.values() if scope == 'all' or spec.scope == scope]


def openai_tools(scope: str = 'all') -> list[dict]:
    return [{'type': 'function', 'function': {
        'name': spec.name, 'description': spec.description, 'parameters': spec.schema,
    }} for spec in _select(scope)]


def anthropic_tools(scope: str = 'all') -> list[dict]:
    return [{'name': spec.name, 'description': spec.description, 'input_schema': spec.schema} for spec in _select(scope)]


def execute(name: str, args: dict, ctx: ToolContext, *, scope: str = 'all') -> tuple[dict, bool]:
    """参数 schema 与模型、MCP 共用；领域校验仍归账本/导入服务。"""
    spec = TOOLS.get(name)
    if spec is None or scope not in ('all', spec.scope):
        return truncate.error_result(f'没有名为 {name} 的工具。', code='UNKNOWN_TOOL'), True
    invalid = next(_VALIDATORS[name].iter_errors(args), None)
    if invalid is not None:
        field = '.'.join(map(str, invalid.absolute_path)) or '参数'
        # 不重复模型传来的大对象。
        message = invalid.message if invalid.validator == 'required' else f'不符合 schema 的 {invalid.validator} 约束。'
        return truncate.error_result(f'{field}：{message}', code='BAD_ARGUMENTS'), True
    try:
        policy = privacy.current()
        if name in {'finance_ledger_read', 'finance_ledger_apply'}:
            args = policy.ledger_refs(args)
        result = spec.handler(args, ctx)
    except brief.BriefError as exc:
        return truncate.error_result(str(exc), code=exc.code), True
    except (ToolInputError, LedgerError, import_tool.ImportError_, sql_tool.SqlRejected) as exc:
        return truncate.error_result(policy.text(exc.message), code=exc.code), True
    except Exception:  # 兜底：内部错误不冒充领域拒绝，也不把本地异常详情发给模型。
        log.exception('AgentCore tool failed: %s', name)
        return truncate.error_result('工具执行异常；请重新读取状态，不要猜测结果。', code='TOOL_CRASH'), True
    return truncate.ok_result(policy.with_candidates(result).redact(result, data_keys=True)), False


def recover(name: str, args: dict, ctx: ToolContext) -> dict | None:
    """只查询已经落盘的回执；进程重启不自动执行工具。"""
    if name in {'finance_ledger_apply', 'finance_report_brief_write'} and ctx.call_key:
        return ledger_tools.committed_receipt(ctx.call_key)
    if name == 'finance_import_commit' and isinstance(args.get('stage_id'), str):
        return import_tool.committed_receipt(args['stage_id'])
    return None
