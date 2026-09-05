from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator


class TargetContent(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    target_kind: Literal["placeholder", "shape"]
    target_id: int = Field(ge=0)
    content: str

    @field_validator("content")
    @classmethod
    def content_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value: raise ValueError("content must not be empty")
        return value

    @property
    def placeholder_id(self) -> int: return self.target_id


PlaceholderContent = TargetContent


class SlideContent(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    slide_number: int = Field(ge=1)
    layout_name: str
    targets: list[TargetContent]
    speaker_notes: str | None = None

    @property
    def placeholders(self) -> list[TargetContent]: return self.targets


class PresentationContent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    presentation_title: str
    slides: list[SlideContent]


class SlideChunkResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slides: list[SlideContent]


class RecoveredTargetContent(TargetContent):
    slide_number: int = Field(ge=1)


class MissingTargetRecoveryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    targets: list[RecoveredTargetContent]


class OrderedTargetRecoveryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contents: list[str]

    @field_validator("contents")
    @classmethod
    def contents_must_not_be_blank(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        if any(not value for value in cleaned):
            raise ValueError("recovered content must not be empty")
        return cleaned


class TemplateSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selected_template_id: str
    confidence: float = Field(ge=0, le=1)
    reason: str
    matched_requirements: list[str]
    unmatched_requirements: list[str]

    @field_validator("selected_template_id", "reason")
    @classmethod
    def selection_text_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value: raise ValueError("value must not be empty")
        return value
