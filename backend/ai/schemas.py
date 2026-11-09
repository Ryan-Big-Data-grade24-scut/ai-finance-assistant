from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class AISettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str = Field(min_length=1, max_length=40)
    base_url: str = Field(min_length=8, max_length=500)
    model: str = Field(min_length=1, max_length=160)
    api_key: str | None = Field(default=None, max_length=2000)
    clear_api_key: bool = False
    auto_apply_rules: bool = False
    allow_external_processing: bool = False
    confidence_threshold: float = Field(default=0.90, ge=0.70, le=0.99)


class AIImportCalibrateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=3000)


class AIImportCommitIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approved_category_paths: list[str] = Field(default_factory=list, max_length=120)
    approved_row_indices: list[int] = Field(default_factory=list, max_length=5000)
    remember_rule_row_indices: list[int] = Field(default_factory=list, max_length=5000)


class AIProposalConfirmIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approved_tx_ids: list[int] = Field(min_length=1, max_length=5000)
    expected_seq: int | None = Field(default=None, ge=0)


class AIProposalResolveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tx_id: int = Field(gt=0)
    target_node_id: str = Field(min_length=1, max_length=160)
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=500)
    user_answer: str = Field(min_length=1, max_length=1000)
    remember_rule: bool = False
    rule_field: str | None = None
    rule_pattern: str | None = Field(default=None, max_length=200)
    expected_seq: int | None = Field(default=None, ge=0)


class AIProposalRejectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=1000)


class AIProposalUndoIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_seq: int | None = Field(default=None, ge=0)


class AIHistoryCommitIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approved_category_paths: list[str] = Field(default_factory=list, max_length=120)
    approved_tx_ids: list[int] = Field(min_length=1, max_length=5000)
    remember_rule_tx_ids: list[int] = Field(default_factory=list, max_length=5000)
