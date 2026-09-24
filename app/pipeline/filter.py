"""Noise filter (FR-009).

Rows where ``is_requirement=false`` are routed to ``filtered_lines`` (never
silently discarded) with their full text preserved for re-inclusion (FR-034).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.pipeline.mine import MinedItem

FILTER_REASON = "model marked is_requirement=false (boilerplate/TOC/narrative)"


@dataclass
class FilterOutcome:
    requirements: list[MinedItem]
    filtered: list[tuple[MinedItem, str]]  # (item, filter_reason)


def split_requirements(items: list[MinedItem]) -> FilterOutcome:
    requirements: list[MinedItem] = []
    filtered: list[tuple[MinedItem, str]] = []
    for item in items:
        if item.is_requirement:
            requirements.append(item)
        else:
            filtered.append((item, FILTER_REASON))
    return FilterOutcome(requirements=requirements, filtered=filtered)
