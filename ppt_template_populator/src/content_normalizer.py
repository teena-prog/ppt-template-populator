from __future__ import annotations

import re
from typing import Any


class BulletNormalizationError(ValueError):
    pass


_BODY_ROLES = {"body", "bullets", "content", "card"}
_SHORT_ROLES = {"short_label", "label", "metric", "caption"}
_MARKED_ITEM = re.compile(r"^\s*(?:[\u2022\u25cf\u25e6\u25aa*-]|\d{1,3}[.)])\s+(.+?)\s*$")
_STANDALONE_MARKER = re.compile(r"^\s*(?:[\u2022\u25cf\u25e6\u25aa*-]|\d{1,3}[.)]?)\s*$")
_TERMINAL = re.compile(r"[.!?][\"')\]]*$")
_LINKING_END = re.compile(r"\b(?:on|to|and|with|from|of|the)\s*$", re.IGNORECASE)
_CARRIED_PREFIX = re.compile(r"^(?:on|to|of|the)\b", re.IGNORECASE)
_INTENTIONAL_LOWERCASE = re.compile(
    r"^(?:e\.g\.|i\.e\.|pH\b|iOS\b|e-commerce\b|https?://|www\.|"
    r"[a-z]+(?:_[a-zA-Z0-9]+|[A-Z][a-zA-Z0-9]*)|[xyz]\b)"
)


def _clear_adjacent_fragment(previous: str, current: str) -> bool:
    """Require multiple mutually supporting signals before joining list items."""
    if _TERMINAL.search(previous) or not current[:1].islower():
        return False
    relationship = bool(_LINKING_END.search(previous) or _CARRIED_PREFIX.match(current))
    return relationship


def _merge_clear_list_fragments(items: list[str]) -> list[str]:
    merged: list[str] = []
    for item in items:
        if merged and _clear_adjacent_fragment(merged[-1], item):
            merged[-1] = f"{merged[-1]} {item}"
        else:
            merged.append(item)
    return merged


def _normalize_leading_capitalization(value: str) -> str:
    if not value[:1].islower() or _INTENTIONAL_LOWERCASE.match(value):
        return value
    return value[0].upper() + value[1:]


def _continues_sentence(previous: str, current: str, *, after_standalone_marker: bool = False) -> bool:
    if after_standalone_marker or _LINKING_END.search(previous):
        return True
    first = current.lstrip()[:1]
    if first and first.islower():
        return True
    if not _TERMINAL.search(previous):
        # Preserve concise, title-cased legacy point lists while joining normal
        # prose lines produced by PDF visual wrapping.
        short_labels = len(previous.split()) <= 3 and len(current.split()) <= 3
        return not (short_labels and first.isupper())
    return False


def _reconstruct_string_items(value: str) -> list[str]:
    text = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []
    items: list[str] = []
    current = ""
    blank_boundary = False
    marker_pending = False
    current_was_marked = False
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            blank_boundary = True
            continue
        marked = _MARKED_ITEM.match(line)
        if marked:
            if current:
                items.append(current)
            current = marked.group(1).strip()
            current_was_marked = True
            marker_pending = False
            blank_boundary = False
            continue
        if _STANDALONE_MARKER.fullmatch(line):
            if current:
                items.append(current)
                current = ""
            marker_pending = True
            current_was_marked = False
            blank_boundary = False
            continue
        if not current:
            current = line
            current_was_marked = marker_pending
        elif current_was_marked or _continues_sentence(current, line, after_standalone_marker=marker_pending):
            current = f"{current} {line}"
        elif blank_boundary and _TERMINAL.search(current) and line[:1].isupper():
            items.append(current)
            current = line
            current_was_marked = False
        else:
            items.append(current)
            current = line
            current_was_marked = False
        marker_pending = False
        blank_boundary = False
    if current:
        items.append(current)
    return items


def _split_bullet_string(value: str) -> list[str]:
    parts = _reconstruct_string_items(value)
    if len(parts) == 1 and ";" in parts[0]:
        clauses = [part.strip() for part in parts[0].split(";") if part.strip()]
        if len(clauses) > 1 and all(len(clause.split()) >= 2 for clause in clauses):
            parts = clauses
    if len(parts) == 1:
        sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", parts[0]) if part.strip()]
        if len(sentences) > 1:
            parts = sentences
    return parts


def normalize_bullets(value: Any, *, role: str = "body", allow_empty: bool = False) -> list[str]:
    """Return clean bullet strings without ever iterating a string as a collection."""
    from_structured_list = isinstance(value, list)
    if isinstance(value, str):
        candidates = _split_bullet_string(value)
    elif isinstance(value, list):
        if not all(isinstance(item, str) for item in value):
            raise BulletNormalizationError("bullets must be strings; nested objects and numeric values are not supported")
        candidates = [item.strip() for item in value if item.strip()]
        if any(_STANDALONE_MARKER.fullmatch(item) for item in candidates):
            raise BulletNormalizationError("bullet arrays must not contain standalone list markers")
        candidates = _merge_clear_list_fragments(candidates)
    else:
        raise BulletNormalizationError("bullets must be a string or an array of strings")

    normalized_role = role.strip().lower().replace(" ", "_")
    cleaned: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        bullet = re.sub(r"\s+", " ", candidate).strip()
        marker = _MARKED_ITEM.match(bullet)
        if marker:
            bullet = marker.group(1).strip()
        if _STANDALONE_MARKER.fullmatch(bullet):
            if from_structured_list:
                raise BulletNormalizationError("bullet arrays must not contain standalone list markers")
            continue
        if not bullet:
            continue
        if normalized_role in _BODY_ROLES and len(bullet) == 1:
            raise BulletNormalizationError("body bullets must not contain single-character fragments")
        if normalized_role in _BODY_ROLES and _LINKING_END.search(bullet):
            raise BulletNormalizationError("body bullets must not end with an incomplete linking word")
        if normalized_role in _BODY_ROLES:
            bullet = _normalize_leading_capitalization(bullet)
        if normalized_role in _SHORT_ROLES and len(bullet.split()) == 1:
            # Explicit short-role policy: grades/options and conventional
            # single-letter variables are legitimate in metric/label targets.
            if not (re.fullmatch(r"[A-Z0-9]", bullet)
                    or bullet in {"x", "y", "z"}
                    or re.fullmatch(r"[A-Z0-9&+.-]{2,12}", bullet)):
                raise BulletNormalizationError("short target content must be a recognized acronym or meaningful label")
        key = bullet.casefold()
        if key not in seen:
            seen.add(key)
            cleaned.append(bullet)
    if not cleaned and not allow_empty:
        raise BulletNormalizationError("bullets must contain at least one nonblank value")
    return cleaned
