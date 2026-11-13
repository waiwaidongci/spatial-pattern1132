from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ReplacementCellInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Field shapes are intentionally loose: the preflight reports every broken
    # row in one response instead of letting Pydantic reject only the first.
    code: Any
    row: Any
    col: Any
    value: Any
    note: Any = None


class ReplacementEdgeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    a: Any
    b: Any


class ReplacementPreflightInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # list size and graph rules are checked in the service so oversized or
    # malformed batches come back as one consolidated broken-row report.
    cells: list[ReplacementCellInput]
    edges: list[ReplacementEdgeInput]


class ReplacementConfirmInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=16, max_length=120)
    confirm: Literal[True]


class BrokenRow(BaseModel):
    row: int | None
    label: str
    code: str | None
    field: str
    message: str
    value: object = None


class PreflightResult(BaseModel):
    status: Literal["ready", "invalid"]
    survey_id: int
    survey_code: str
    summary: dict
    broken_rows: list[BrokenRow]
    token: str | None = None
    replacement_id: int | None = None
    batch_hash: str | None = None
    source_state_hash: str | None = None
    cell_count: int | None = None
    edge_count: int | None = None
    preserved_notes: int | None = None
    removed_codes: list[str] | None = None
    added_codes: list[str] | None = None
    graph: dict | None = None
    rule: str | None = None
    runs_on_period: list[dict] | None = None


class ReplacementSummary(BaseModel):
    replacement_id: int
    survey_id: int
    status: str
    batch_hash: str
    source_state_hash: str
    cell_count: int
    edge_count: int
    preserved_notes: int
    submitted_by: str
    created_at: str
    confirmed_by: str
    confirmed_at: str
