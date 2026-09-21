from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from .content_normalizer import normalize_bullets


class TargetContent(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    target_kind: Literal["placeholder", "shape"]
    target_id: int = Field(ge=0)
    content_type: Literal["title", "bullets"]
    text: str | None = None
    bullets: list[str] | None = None

    @model_validator(mode="before")
    @classmethod
    def legacy_content(cls, value):
        if isinstance(value, dict) and "content" in value:
            copied = dict(value); content = copied.pop("content")
            if copied.get("content_type") == "bullets" or isinstance(content, list):
                copied.update(content_type="bullets", bullets=normalize_bullets(content))
            elif copied.get("content_type") == "title":
                if not isinstance(content, str): raise ValueError("title content must be a string")
                copied["text"] = content.strip()
            elif "content_type" not in copied:
                if not isinstance(content, str): raise ValueError("legacy title content must be a string")
                lines = normalize_bullets(content)
                if len(lines) > 1: copied.update(content_type="bullets", bullets=lines)
                else: copied.update(content_type="title", text=content.strip())
            return copied
        return value

    @model_validator(mode="after")
    def content_shape(self):
        if self.content_type == "title":
            self.text = (self.text or "").strip()
            if not self.text or self.bullets is not None: raise ValueError("title content requires nonblank text only")
        else:
            self.bullets = normalize_bullets(self.bullets)
            if not self.bullets or self.text is not None: raise ValueError("bullet content requires a nonempty bullets array only")
        return self

    @property
    def content(self) -> str:
        return self.text if self.content_type == "title" else "\n".join(self.bullets or [])

    @property
    def placeholder_id(self) -> int: return self.target_id


PlaceholderContent = TargetContent


class SlideContent(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    slide_number: int = Field(ge=1)
    layout_name: str
    section_type: str | None = None
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


class SemanticSlideContent(BaseModel):
    """Strict watsonx-facing content with no template implementation details."""
    model_config = ConfigDict(extra="forbid")
    slide_number: int = Field(ge=1)
    section_type: str
    title: str
    bullets: list[str]
    speaker_notes: str | None = None

    @field_validator("section_type", "title")
    @classmethod
    def semantic_text_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value must not be empty")
        return value

    @field_validator("bullets")
    @classmethod
    def semantic_bullets_are_strings(cls, values: list[str]) -> list[str]:
        return normalize_bullets(values, allow_empty=True)


class SemanticSlideResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slides: list[SemanticSlideContent]


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
