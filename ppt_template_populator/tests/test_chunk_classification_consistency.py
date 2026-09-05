"""Regression test for the 'Unknown shape N on slide M' schema-validation
failure: required/replaceable classification (in particular "repeated
chrome" detection) must give the exact same answer whether it is computed
against the full deck or against the small per-chunk slide subset the
pipeline validates generated content against. If the two ever disagree, the
model -- prompted using the full-template classification -- returns targets
the subset-based validator then rejects as unknown, or vice versa."""
from pathlib import Path

from src.pipeline import GenerationPipeline
from src.target_metadata import required_targets_by_slide


def _chrome_template():
    def slide(number):
        return {
            "slide_number": number,
            "layout_name": "Blank",
            "targets": [
                {
                    "slide_number": number, "target_kind": "shape", "target_id": 1, "role": "body",
                    "existing_text": f"Unique point {number}", "approximate_max_characters": 100,
                    "max_content_length": 100, "required": False, "replaceable": True,
                    "position": {"left": 0, "top": 0, "width": 100, "height": 20},
                },
                # Identical wording repeated verbatim on every slide: only
                # visible as "repeated chrome" (and therefore non-required)
                # once the whole deck is classified at once.
                {
                    "slide_number": number, "target_kind": "shape", "target_id": 2, "role": "body",
                    "existing_text": "Company confidential", "approximate_max_characters": 100,
                    "max_content_length": 100, "required": False, "replaceable": True,
                    "position": {"left": 0, "top": 30, "width": 100, "height": 20},
                },
            ],
        }

    return {"template_id": "chrome-deck", "slide_count": 3, "slides": [slide(n) for n in (1, 2, 3)]}


def test_full_template_excludes_repeated_chrome_from_required_targets():
    full_required = required_targets_by_slide(_chrome_template())
    assert set(full_required[1]) == {("shape", 1)}


def test_chunk_subset_classification_matches_full_template_classification():
    template = _chrome_template()
    full_required = required_targets_by_slide(template)
    pipeline = GenerationPipeline(None, None, Path("."), Path("."))

    for numbers in ({1}, {2}, {3}, {1, 2}, {2, 3}, {1, 2, 3}):
        subset = pipeline._template_subset(template, numbers)
        subset_required = required_targets_by_slide(subset)
        for slide_number in numbers:
            assert subset_required[slide_number] == full_required[slide_number], (
                f"slide {slide_number} in subset {numbers} disagreed with the full-template classification"
            )
