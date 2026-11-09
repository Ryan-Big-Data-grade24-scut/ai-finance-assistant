"""Typed contracts for Penny's lightweight Agent Runtime."""
from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class AgentStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    tool: str = Field(min_length=1, max_length=100)
    purpose: str = Field(default="", max_length=240)
    arguments: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list, max_length=12)

    @field_validator("id", "tool")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("字段不能为空")
        return value

    @field_validator("id")
    @classmethod
    def safe_step_id(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
            raise ValueError("step id 只能包含英文字母、数字、下划线和连字符。")
        return value


class AgentPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str = Field(default="", max_length=500)
    intent: str = Field(default="", max_length=120)
    needs_clarification: bool = False
    clarification_question: str | None = Field(default=None, max_length=500)
    steps: list[AgentStep] = Field(default_factory=list, max_length=12)
    replan_after_execution: bool = True
    preference_updates: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_clarification(self):
        if self.needs_clarification and not self.clarification_question:
            raise ValueError("needs_clarification=true 时必须提供 clarification_question")
        if self.needs_clarification and self.steps:
            raise ValueError("需要澄清时不能同时执行工具步骤")
        return self


class StepExecution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: str
    tool: str
    arguments: dict[str, Any]
    status: str
    result: dict[str, Any] | None = None
    error: str | None = None
    duration_ms: int = 0


class VerificationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verified: bool
    partial: bool = False
    issues: list[str] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    usable_results: list[dict[str, Any]] = Field(default_factory=list)
