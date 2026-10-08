'Bounded list results: paging for `list_entities`, and a byte cap for list-returning tools.'
from __future__ import annotations

import json
from typing import Any

DEFAULT_LIMIT = 50
MAX_BYTES = 60_000


def _size(item: Any) -> int:
    return len(json.dumps(item, separators=(",", ":"), default=str))


def _fit(items: list, budget: int) -> int:
    'How many leading items of `items` serialize inside `budget` bytes (as a JSON array),\n    and always at least one, so a single oversize item is returned whole, not dropped.'
    used = 2                                   
    for n, item in enumerate(items):
        used += _size(item) + (1 if n else 0)  
        if used > budget:
            return max(n, 1)
    return len(items)


def page(items: list, limit: int = DEFAULT_LIMIT, offset: int = 0,
         max_bytes: int | None = None) -> dict:
    '`{items, total, shown, next}` for one page of `items`; `next` is the next offset or\n    None. `limit=0` means no limit, and `truncated: True` appears when the byte cap cut the page.'
    total = len(items)
    chosen = items[offset:] if limit == 0 else items[offset:offset + limit]
    fit = _fit(chosen, MAX_BYTES if max_bytes is None else max_bytes)
    out: dict = {"items": chosen[:fit], "total": total, "shown": fit,
                 "next": offset + fit if offset + fit < total else None}
    if fit < len(chosen):
        out["truncated"] = True
    return out


def cap(value: Any, max_bytes: int | None = None, key: str | None = None) -> Any:
    '`value` unchanged when its JSON fits `max_bytes`. Otherwise fewer items, `truncated: True`,\n    `total`, `shown` and `next`: a list becomes `{items, ...}`; a dict keeps its shape and its\n    list under `key` is the part trimmed.'
    budget = MAX_BYTES if max_bytes is None else max_bytes
    if isinstance(value, list):
        if _size(value) <= budget:
            return value
        return page(value, limit=0, max_bytes=budget)
    if isinstance(value, dict) and key and isinstance(value.get(key), list):
        rows = value[key]
        rest = {k: v for k, v in value.items() if k != key}
        
        room = budget - _size(rest) - len(key) - 4 - 50
        if _size(value) <= budget:
            return value
        fit = _fit(rows, room)
        if fit >= len(rows):
            return value
        return {**value, key: rows[:fit], "shown": fit, "truncated": True,
                "next": fit}
    return value
