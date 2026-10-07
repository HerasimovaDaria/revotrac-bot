"""Helpers shared by the subscribe and day-off selectors (Discord select-menu chunking)."""

from config import log
from db import _all_members

SELECT_LIMIT      = 25   # Discord: max options per select
MAX_SELECTS       = 4    # Discord: 5 rows per view, one is taken by the buttons


def _member_chunks(context: str) -> list[list[tuple[str, str, float, float]]]:
    """Split all members into chunks of SELECT_LIMIT (at most MAX_SELECTS chunks)."""
    all_m = _all_members()
    if len(all_m) > SELECT_LIMIT * MAX_SELECTS:
        log.warning("Too many members (%d) — only first %d shown in %s",
                    len(all_m), SELECT_LIMIT * MAX_SELECTS, context)
    return [all_m[i:i + SELECT_LIMIT]
            for i in range(0, min(len(all_m), SELECT_LIMIT * MAX_SELECTS), SELECT_LIMIT)]


def _chunk_placeholder(default: str, i: int, chunk: list, total_chunks: int) -> str:
    if total_chunks == 1:
        return default
    return f"People {i * SELECT_LIMIT + 1}–{i * SELECT_LIMIT + len(chunk)}…"
