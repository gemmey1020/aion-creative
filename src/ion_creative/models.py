"""Typed results shared by the Ion Creative tools and runtime adapters."""

from typing import Literal

from pydantic import BaseModel, Field


class IonToolError(BaseModel):
    engine: str
    stage: str
    code: str
    message: str


class IonToolResult(BaseModel):
    status: str
    tool: str
    message: str
    output_paths: list[str] = Field(default_factory=list)
    engines: list[str] = Field(default_factory=list)
    exact_versions: dict[str, str] = Field(default_factory=dict)
    preview_paths: list[str] = Field(default_factory=list)
    qa_needed: bool = True
    errors: list[IonToolError] = Field(default_factory=list)


class EngineInspection(BaseModel):
    engine: str
    version: str | None = None
    healthy: bool = False
    capabilities: list[str] = Field(default_factory=list)
    capability_count: int = 0
    launcher_state: str = "UNKNOWN"
    errors: list[IonToolError] = Field(default_factory=list)


class RuntimeInspection(BaseModel):
    status: Literal["NOT_READY", "HEALTHY", "DEGRADED"]
    engines: list[EngineInspection] = Field(default_factory=list)
    message: str = ""
