from __future__ import annotations

from typing import Any
from pydantic import BaseModel, ConfigDict, Field, model_validator


class TemplateProfile(BaseModel):
    """Canonical complete template contract used after either selection mode."""
    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)
    template_id: str = Field(min_length=1)
    template_name: str = Field(min_length=1)
    slide_count: int = Field(gt=0)
    slides: list[dict[str, Any]] = Field(min_length=1)
    checksum_sha256: str = Field(min_length=64, max_length=64)
    minio_bucket: str = Field(min_length=1)
    minio_object_key: str = Field(min_length=1)
    pptx_binary_bytes: bytes

    @model_validator(mode="after")
    def complete_profile(self) -> "TemplateProfile":
        if len(self.slides) != self.slide_count:
            raise ValueError("Template slide metadata does not match slide_count.")
        for slide in self.slides:
            if not isinstance(slide.get("slide_number"), int) or not isinstance(slide.get("targets"), list):
                raise ValueError("Template slide metadata is incomplete.")
        if not self.pptx_binary_bytes:
            raise ValueError("Template binary is empty.")
        return self


def canonical_template_profile(document: dict[str, Any]) -> TemplateProfile:
    return TemplateProfile.model_validate(document)


def csv_values(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def build_template_profile(metadata: dict[str, Any]) -> str:
    layouts = sorted({slide["layout_name"] for slide in metadata["slides"]})
    capacities = [target["max_content_length"] for slide in metadata["slides"] for target in slide.get("targets", [])]
    capacity = sum(capacities)
    fields = [
        f"Template: {metadata['template_name']}", f"Description: {metadata.get('description', '')}",
        f"Category: {metadata.get('category', '')}", f"Use cases: {', '.join(metadata.get('use_cases', []))}",
        f"Target audiences: {', '.join(metadata.get('target_audiences', []))}", f"Tones: {', '.join(metadata.get('tones', []))}",
        f"Visual style: {metadata.get('visual_style', '')}", f"Supported sections: {', '.join(metadata.get('supported_sections', []))}",
        f"Slide count: {metadata['slide_count']}", f"Layouts: {', '.join(layouts)}",
        f"Image support: {metadata.get('has_image_placeholders', False)}", f"Chart support: {metadata.get('has_chart_placeholders', False)}",
        f"Approximate total text capacity: {capacity} characters",
        f"Usable placeholders: {metadata.get('usable_placeholder_count', 0)}",
        f"Usable ordinary text targets: {metadata.get('usable_shape_target_count', 0)}",
        f"Slides without writable targets: {metadata.get('slides_without_writable_targets', 0)}",
        f"Safe for automatic population: {metadata.get('safe_for_automatic_population', False)}",
    ]
    return ". ".join(fields)
