from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ExtractedDocumentResponse(BaseModel):
    title: str
    text: str
    warnings: list[str] = Field(default_factory=list)


class TemplateSummary(BaseModel):
    template_id: str
    template_name: str
    category: str | None = None
    slide_count: int = 0
    safe_for_automatic_population: bool = False
    retrieval_score: float | None = None
    requirement_match_score: float | None = None
    rerank_score: float | None = None


class SelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    topic: str = Field(min_length=1, max_length=500)
    source_content: str = Field(min_length=1)
    audience: str = Field(default="General audience", max_length=500)
    tone: str = Field(default="Professional", max_length=200)
    required_sections: list[str] = Field(default_factory=list)
    visual_preferences: str = Field(default="", max_length=2000)
    user_instructions: str = Field(default="", max_length=5000)
    desired_slide_count: int | None = Field(default=14, ge=2, le=30)

    @field_validator("topic", "source_content")
    @classmethod
    def required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class SelectionResponse(BaseModel):
    selected_template_id: str
    confidence: float
    reason: str
    matched_requirements: list[str]
    unmatched_requirements: list[str]
    candidates: list[TemplateSummary]


class GenerationRequest(SelectionRequest):
    selection_mode: Literal["automatic", "manual"] = "automatic"
    template_id: str | None = None

    @field_validator("template_id")
    @classmethod
    def clean_template_id(cls, value: str | None) -> str | None:
        return value.strip() if value else None

    @model_validator(mode="after")
    def manual_requires_template(self) -> "GenerationRequest":
        if self.selection_mode == "manual" and not self.template_id:
            raise ValueError("template_id is required for manual selection")
        return self


class JobCreatedResponse(BaseModel):
    job_id: str
    status: Literal["queued"] = "queued"


class JobStatusResponse(BaseModel):
    job_id: str
    status: Literal["queued", "running", "completed", "failed"]
    stages: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None
    download_url: str | None = None


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    elasticsearch: str
    minio: str
    watsonx: str
