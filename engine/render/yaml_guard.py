"""Shared guard against YAML alias bombs.

``yaml.safe_load`` resolves aliases as shared references, so a tiny
document (a deep alias chain fanning out per level) can explode into an
astronomical structure when traversed, re-dumped, or rendered. Every
``yaml.safe_load`` entry point that accepts user-supplied YAML must run
:func:`reject_aliased_graphs` on the result before using it.
"""
from __future__ import annotations

from typing import Any


def reject_aliased_graphs(node: Any) -> None:
    """Reject YAML alias bombs: shared or cyclic mutable subgraphs.

    Walk the object graph and reject any dict/list seen twice (an alias or
    a cycle). Anchors-free documents build fresh objects per node and
    repeated scalar VALUES are interned/fine, so legitimate YAML is
    unaffected.
    """
    seen: set[int] = set()
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if id(cur) in seen:
                raise ValueError("YAML aliases/cyclic structures are not supported.")
            seen.add(id(cur))
            stack.extend(cur.keys())
            stack.extend(cur.values())
        elif isinstance(cur, (list, tuple)):
            if id(cur) in seen:
                raise ValueError("YAML aliases/cyclic structures are not supported.")
            seen.add(id(cur))
            stack.extend(cur)
