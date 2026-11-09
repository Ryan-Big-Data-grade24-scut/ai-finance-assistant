"""Single registry for Penny's model-facing tools and their execution contract."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Callable


class ToolRegistryError(ValueError):
    pass


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    read_only: bool
    proposal_only: bool


ToolHandler = Callable[[str, dict[str, Any], str, dict[str, str] | None], dict[str, Any]]


class ToolRegistry:
    """Owns tool schemas, policy metadata, provider adapters, and dispatch."""

    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._handler: ToolHandler | None = None

    def set_handler(self, handler: ToolHandler) -> None:
        self._handler = handler

    def register(
        self,
        definition: dict[str, Any],
        *,
        read_only: bool,
        proposal_only: bool = False,
    ) -> None:
        name = str(definition.get("name") or "").strip()
        if not name or name in self._specs:
            raise ToolRegistryError(f"工具名称为空或重复：{name!r}")
        parameters = definition.get("parameters")
        if not isinstance(parameters, dict) or parameters.get("type") != "object":
            raise ToolRegistryError(f"工具 {name} 必须使用 object 参数 Schema。")
        self._specs[name] = ToolSpec(
            name=name,
            description=str(definition.get("description") or ""),
            parameters=parameters,
            read_only=bool(read_only),
            proposal_only=bool(proposal_only),
        )

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def names(self) -> set[str]:
        return set(self._specs)

    def specs(self) -> list[ToolSpec]:
        return list(self._specs.values())

    def validate_arguments(self, name: str, arguments: dict[str, Any], *, allow_refs: bool = False) -> None:
        spec = self._specs.get(name)
        if spec is None:
            raise ToolRegistryError(f"未注册的工具：{name}")
        if not isinstance(arguments, dict):
            raise ToolRegistryError(f"工具 {name} 的参数必须是 JSON 对象。")
        _validate_value(arguments, spec.parameters, f"{name}.arguments", allow_refs=allow_refs)

    def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        conversation_id: str,
        context: dict[str, str] | None = None,
        *,
        allow_proposals: bool = False,
    ) -> dict[str, Any]:
        spec = self._specs.get(name)
        if spec is None:
            raise ToolRegistryError(f"模型请求了未注册的工具：{name}")
        if spec.proposal_only and not allow_proposals:
            raise ToolRegistryError("当前任务没有明确要求修改账本，不能创建写入提案。")
        self.validate_arguments(name, arguments)
        if self._handler is None:
            raise ToolRegistryError("工具执行器尚未初始化。")
        result = self._handler(name, arguments, conversation_id, context)
        if not isinstance(result, dict):
            raise ToolRegistryError(f"工具 {name} 返回了非对象结果。")
        return result

    def openai_tools(self, *, allow_proposals: bool = False) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.parameters,
                },
            }
            for spec in self._specs.values()
            if allow_proposals or not spec.proposal_only
        ]

    def anthropic_tools(self, *, allow_proposals: bool = False) -> list[dict[str, Any]]:
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "input_schema": spec.parameters,
            }
            for spec in self._specs.values()
            if allow_proposals or not spec.proposal_only
        ]

    def planner_catalog(self, *, allow_proposals: bool = False) -> list[dict[str, Any]]:
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.parameters,
                "read_only": spec.read_only,
                "proposal_only": spec.proposal_only,
            }
            for spec in self._specs.values()
            if allow_proposals or not spec.proposal_only
        ]


def is_reference(value: Any) -> bool:
    return isinstance(value, dict) and set(value) == {"$ref"} and isinstance(value.get("$ref"), str)


def reference_paths(value: Any) -> list[str]:
    found: list[str] = []
    if is_reference(value):
        found.append(str(value["$ref"]))
    elif isinstance(value, dict):
        for item in value.values():
            found.extend(reference_paths(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(reference_paths(item))
    return found


def _validate_value(value: Any, schema: dict[str, Any], path: str, *, allow_refs: bool) -> None:
    if allow_refs and is_reference(value):
        ref = str(value["$ref"])
        if not re.fullmatch(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+|\.\d+)*", ref):
            raise ToolRegistryError(f"{path} 的 $ref 格式不合法。")
        return

    expected = schema.get("type")
    type_ok = {
        "object": lambda v: isinstance(v, dict),
        "array": lambda v: isinstance(v, list),
        "string": lambda v: isinstance(v, str),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
        "boolean": lambda v: isinstance(v, bool),
        "null": lambda v: v is None,
    }
    if expected in type_ok and not type_ok[expected](value):
        raise ToolRegistryError(f"{path} 类型错误，应为 {expected}。")

    if "enum" in schema and value not in schema["enum"]:
        raise ToolRegistryError(f"{path} 不在允许值范围内。")
    if isinstance(value, str):
        if len(value) < int(schema.get("minLength", 0)):
            raise ToolRegistryError(f"{path} 长度不足。")
        if "maxLength" in schema and len(value) > int(schema["maxLength"]):
            raise ToolRegistryError(f"{path} 长度超限。")
        pattern = schema.get("pattern")
        if pattern and not re.fullmatch(str(pattern), value):
            raise ToolRegistryError(f"{path} 格式不合法。")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not math.isfinite(value):
            raise ToolRegistryError(f"{path} 必须是有限数值。")
        if "minimum" in schema and value < schema["minimum"]:
            raise ToolRegistryError(f"{path} 小于允许的最小值。")
        if "maximum" in schema and value > schema["maximum"]:
            raise ToolRegistryError(f"{path} 大于允许的最大值。")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            raise ToolRegistryError(f"{path} 必须大于 {schema['exclusiveMinimum']}。")

    if isinstance(value, dict):
        if "maxProperties" in schema and len(value) > int(schema["maxProperties"]):
            raise ToolRegistryError(f"{path} 属性数量超限。")
        properties = schema.get("properties") or {}
        for required in schema.get("required") or []:
            if required not in value:
                raise ToolRegistryError(f"{path} 缺少必填参数 {required}。")
        if schema.get("additionalProperties", False) is False:
            unknown = set(value) - set(properties)
            if unknown:
                raise ToolRegistryError(f"{path} 包含未知参数：{', '.join(sorted(unknown))}")
        for key, item in value.items():
            child_schema = properties.get(key)
            if isinstance(child_schema, dict):
                _validate_value(item, child_schema, f"{path}.{key}", allow_refs=allow_refs)
    if isinstance(value, list):
        if "minItems" in schema and len(value) < int(schema["minItems"]):
            raise ToolRegistryError(f"{path} 项目数量不足。")
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            raise ToolRegistryError(f"{path} 项目数量超限。")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _validate_value(item, item_schema, f"{path}[{index}]", allow_refs=allow_refs)
