"""Result-size limits so tool output stays small and predictable for the LLM."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


def capped[T](items: Sequence[T], limit: int) -> dict[str, Any]:
    """Wrap a list result with a count and an explicit truncation flag."""
    shown = list(items[:limit])
    return {"count": len(shown), "truncated": len(items) > limit, "items": shown}


def clamp(value: int | None, default: int, maximum: int) -> int:
    """Clamp a caller-supplied ``top``/``limit`` into ``1..maximum``."""
    if value is None:
        return min(default, maximum)
    return max(1, min(value, maximum))
