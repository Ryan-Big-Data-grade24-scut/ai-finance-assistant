from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


RuleField = Literal["counterparty", "item", "pay_method", "source"]


class ClassificationSuggestion(BaseModel):
    """外部 AI 提交的一条候选归位；此模型本身不会修改账本。"""

    model_config = ConfigDict(extra="forbid")

    tx_id: int = Field(gt=0, description="交易 ID")
    target_node_id: str | None = Field(
        default=None,
        description="候选目标 group 节点；不确定时必须为 null",
    )
    confidence: float = Field(ge=0, le=1, description="0~1 的置信度")
    reason: str = Field(min_length=1, max_length=500, description="给用户看的判断依据")
    needs_clarification: bool = Field(
        default=False,
        description="是否必须先向用户提问；true 的条目不能被确认",
    )
    question: str | None = Field(
        default=None,
        max_length=300,
        description="需要用户回答的一个具体问题",
    )
    remember_rule: bool = Field(
        default=False,
        description="用户确认后是否保存为可复用规则",
    )
    rule_field: RuleField | None = None
    rule_pattern: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def validate_workflow(self):
        if self.needs_clarification:
            if self.target_node_id is not None:
                raise ValueError("需要澄清时 target_node_id 必须为 null")
            if not (self.question or "").strip():
                raise ValueError("需要澄清时必须给出 question")
            if self.remember_rule:
                raise ValueError("未澄清的建议不能保存规则")
        elif self.target_node_id is None:
            raise ValueError("明确建议必须提供 target_node_id")

        if self.remember_rule:
            if not self.rule_field or not (self.rule_pattern or "").strip():
                raise ValueError("remember_rule=true 时必须提供 rule_field 和 rule_pattern")
        elif self.rule_field is not None or self.rule_pattern is not None:
            raise ValueError("未保存规则时不要传 rule_field/rule_pattern")
        return self
