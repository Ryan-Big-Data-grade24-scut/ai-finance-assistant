"""Apply per-ledger aliases at the boundary between local records and AI."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
from contextlib import closing
from typing import Any

from backend import db


_SURNAMES = (
    "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜"
    "戚谢邹喻柏水窦章云苏潘葛奚范彭鲁韦昌马苗方俞任袁柳史唐费薛雷贺"
    "倪汤滕殷罗毕郝邬安常乐于傅齐康伍余顾孟平黄和穆萧尹姚邵汪毛宋"
    "熊舒屈项董梁杜阮蓝季贾路江童颜郭梅林钟徐邱骆高夏蔡田樊胡霍万"
    "柯卢莫房裘缪干解应宗丁宣邓单洪包诸左石崔程邢裴陆荣翁许封芮靳"
    "储段祝刘景詹龙叶幸黎白蒲卓蒙池乔谭翟肖阎曾廖欧赖龚侯邰文覃麦"
)
_NAME = re.compile(
    rf"(?:[{_SURNAMES}]|欧阳|司马|上官|诸葛|司徒|慕容|东方|夏侯|皇甫)[\u3400-\u9fff]{{1,2}}"
)
_BUSINESS = re.compile(
    "食堂|餐厅|餐饮|酒楼|火锅|小吃|奶茶|咖啡|饭店|烧烤|商店|商城|商行|超市|网店|"
    "旗舰店|便利店|小卖部|专卖店|店$|小铺|药房|医院|学校|大学|公司|集团|"
    "银行|支付|淘宝|京东|美团|天猫|拼多多|菜鸟|快递|书店|客栈|酒店|网吧|工厂|"
    "诊所|牙科|花店|会员|公交|地铁|物业|停车|食府|面馆|烧腊|食杂|百货|[馆铺摊]$"
)
_COUNTERPARTY_FIELDS = {"counterparty", "merchant", "merchant_name", "交易对方", "对方名称", "收款方", "付款方"}
_PRIVATE_FIELDS = {"对方账号", "对方账户", "对手账号", "对手账户", "银行卡号", "卡号", "身份证号", "手机号", "联系电话"}


def _looks_private(value: str) -> bool:
    return bool(_NAME.fullmatch(value) and not _BUSINESS.search(value))


def _names_in(value: Any) -> set[str]:
    names: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key in _COUNTERPARTY_FIELDS and isinstance(child, str) and _looks_private(child.strip()):
                names.add(child.strip())
            elif isinstance(child, (dict, list)):
                names.update(_names_in(child))
    elif isinstance(value, list):
        for child in value:
            names.update(_names_in(child))
    return names


def _auto_alias(seed: str, original: str) -> str:
    token = hmac.new(seed.encode(), original.encode(), hashlib.sha256).hexdigest()
    return f"联系人-{token[:10].upper()}"


class Policy:
    def __init__(self, seed: str, overrides: dict[str, str], candidates: set[str]):
        self.seed = seed
        self.overrides = overrides
        self.candidates = candidates
        self.aliases = {name: _auto_alias(seed, name) for name in candidates}
        self.aliases.update(overrides)
        self.aliases = {name: alias for name, alias in self.aliases.items() if alias}
        self._replacements = {name.casefold(): alias for name, alias in self.aliases.items()}
        self._replacements.update({
            _auto_alias(seed, name).casefold(): overrides[name]
            for name in candidates & overrides.keys() if overrides[name]
        })
        terms = self._replacements.keys()
        if terms:
            # Protect aliases already in a message so successive passes are idempotent.
            aliases = set(self.aliases.values())
            self._pattern = re.compile("|".join(map(re.escape, sorted(set(terms) | aliases, key=len, reverse=True))), re.I)
        else:
            self._pattern = None
        self._reverse = {alias.casefold(): name for name, alias in self.aliases.items()}
        self._alias_pattern = (re.compile("|".join(map(re.escape, sorted(self._reverse, key=len, reverse=True))), re.I)
                               if self._reverse else None)

    def with_candidates(self, value: Any) -> Policy:
        names = _names_in(value) - self.candidates - self.overrides.keys()
        return Policy(self.seed, self.overrides, self.candidates | names) if names else self

    def text(self, value: str) -> str:
        if self._pattern:
            return self._pattern.sub(lambda m: self._replacements.get(m[0].casefold(), m[0]), value)
        return value

    def redact(self, value: Any, *, data_keys: bool = False) -> Any:
        if isinstance(value, str):
            if value.lstrip().startswith(("{", "[")):
                try:
                    parsed = json.loads(value)
                except ValueError:
                    pass
                else:
                    cleaned = self.redact(parsed, data_keys=data_keys)
                    return value if parsed == cleaned else json.dumps(cleaned, ensure_ascii=False)
            return self.text(value)
        if isinstance(value, list):
            return [self.redact(item, data_keys=data_keys) for item in value]
        if isinstance(value, dict):
            return {
                (self.text(key) if data_keys and isinstance(key, str) else key):
                ("[隐私信息]" if data_keys and key in _PRIVATE_FIELDS and item else
                 self.redact(item, data_keys=data_keys))
                for key, item in value.items()
            }
        return value

    def messages(self, messages: list[dict]) -> list[dict]:
        result = []
        for message in messages:
            item = dict(message)
            if "content" in item:
                item["content"] = self.redact(item["content"], data_keys=item["role"] == "tool")
            if "reasoning_content" in item:
                item["reasoning_content"] = self.redact(item["reasoning_content"])
            if item.get("tool_calls"):
                item["tool_calls"] = self.redact(item["tool_calls"])
            if item.get("anthropic_blocks"):
                item["anthropic_blocks"] = self.signed_blocks(item["anthropic_blocks"])
            result.append(item)
        return result

    def signed_blocks(self, blocks: list[dict]) -> list[dict]:
        clean = []
        for block in blocks:
            kind = block.get("type")
            if kind == "redacted_thinking":
                # Opaque provider data cannot be inspected for private text.
                continue
            if kind == "thinking":
                if self.text(block.get("thinking", "")) == block.get("thinking", ""):
                    clean.append(block)
            else:
                clean.append(self.redact(block))
        return clean

    def resolve(self, value: str) -> str:
        if self._alias_pattern:
            return self._alias_pattern.sub(lambda m: self._reverse.get(m[0].casefold(), m[0]), value)
        return value

    def ledger_refs(self, value: Any) -> Any:
        """Category references can contain a label. Resolve them only for local ledger lookup."""
        if isinstance(value, list):
            return [self.ledger_refs(item) for item in value]
        if isinstance(value, dict):
            return {
                key: self.ledger_refs(item) if key not in {"name", "title", "note"} else item
                for key, item in value.items()
            }
        if isinstance(value, str) and value.startswith("cat:"):
            return "cat:" + self.resolve(value[4:])
        return value


def _load(conn: sqlite3.Connection) -> tuple[Policy, dict[str, int]]:
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('ai_privacy_seed', 'transactions_raw')"
    )}
    counts = {
        row[0]: row[1] for row in conn.execute(
            "SELECT trim(counterparty), COUNT(*) FROM transactions_raw "
            "WHERE counterparty IS NOT NULL AND trim(counterparty) <> '' GROUP BY trim(counterparty)"
        )
    } if "transactions_raw" in tables else {}
    seed = ""
    overrides = {}
    if "ai_privacy_seed" in tables:
        seed = conn.execute("SELECT salt FROM ai_privacy_seed WHERE id=1").fetchone()[0]
        overrides = dict(conn.execute("SELECT original, alias FROM ai_privacy_alias").fetchall())
    return Policy(seed, overrides, {name for name in counts if _looks_private(name)}), counts


def current(conn: sqlite3.Connection | None = None) -> Policy:
    if conn is not None:
        return _load(conn)[0]
    with closing(db.get_conn()) as local:
        return _load(local)[0]


def _entries(policy: Policy, counts: dict[str, int]) -> list[dict]:
    return [
        {"original": name, "alias": policy.aliases.get(name, ""), "count": counts.get(name, 0),
         "mode": ("keep" if policy.overrides[name] == "" else "custom") if name in policy.overrides else "auto",
         "detected": name in policy.candidates}
        for name in sorted(policy.candidates | policy.overrides.keys(), key=lambda name: (-counts.get(name, 0), name))
    ]


def settings() -> list[dict]:
    with closing(db.get_conn()) as conn:
        policy, counts = _load(conn)
        return _entries(policy, counts)


def save_alias(original: str, alias: str | None) -> list[dict]:
    original = original.strip()
    alias = alias.strip() if alias is not None else None
    if not original:
        raise ValueError("请输入要隐藏的原文。")
    with closing(db.get_conn()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        policy, counts = _load(conn)
        overrides = {name: value for name, value in policy.overrides.items() if name.casefold() != original.casefold()}
        if alias is not None:
            if alias:
                others = Policy(policy.seed, overrides, policy.candidates - {original})
                if original.casefold() in alias.casefold() or others.text(alias) != alias:
                    raise ValueError("请使用不包含隐私原文的别名。")
                if alias.casefold() in {name.casefold() for name in counts} or any(
                    alias.casefold() in value.casefold() or value.casefold() in alias.casefold()
                    for value in others.aliases.values()
                ):
                    raise ValueError("这个别名已在使用，请换一个。")
                if any(original.casefold() in value.casefold() for value in overrides.values() if value):
                    raise ValueError("原文与现有别名重叠，请先修改已有别名。")
            overrides[original] = alias
        conn.execute("DELETE FROM ai_privacy_alias WHERE original = ?", (original,))
        if alias is not None:
            conn.execute("INSERT INTO ai_privacy_alias(original, alias) VALUES (?, ?)", (original, alias))
        return _entries(Policy(policy.seed, overrides, policy.candidates), counts)
