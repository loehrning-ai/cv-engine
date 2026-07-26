"""Deterministically apply tailoring suggestions onto a CV model (Stage 6).

The LLM returns STRUCTURED suggestions (see :mod:`engine.tailor.prompts`); it
does NOT rewrite the CV. This module is the deterministic, offline applier that
turns those typed operations into a new CV model — never mutating the original
(immutable: it deep-copies and returns a fresh dict).

Why a deterministic applier rather than trusting a model-written CV:
  * the original CV is preserved exactly (duplicate-then-tailor) — only a copy
    is transformed, and the source row is never touched;
  * every op is addressed by section-key + original index, so we can validate
    each one and silently SKIP a malformed / out-of-range / ungrounded op
    rather than corrupting the document;
  * the result still flows through the anti-fabrication cross-reference and the
    1-page gate downstream — this applier is one link, not the only guard.

Supported ops (subset of the dossier's vocabulary that maps to a safe, local
edit): ``reorder_sections``, ``reorder_items``, ``reorder_bullets``,
``rewrite_bullet``, ``add_emphasis``, ``reorder_skill_items``, and
``retag_stack``. Content-changing ops require an exact ``before`` source anchor.
Unknown ops are ignored.

All functions here are pure and side-effect-free; ``apply_suggestions``
returns ``(new_cv, applied_ids, skipped)`` so the service can report what
actually changed.
"""
from __future__ import annotations

import copy
from typing import Any

# Top-level keys that are identity/styling and must NEVER be reordered or
# rewritten by a tailoring op (dossier §2).
_PROTECTED_KEYS = frozenset(
    {"name", "accent", "font", "density", "contact", "photo", "summary"}
)


def _is_section_list(value: object) -> bool:
    """True iff ``value`` is a tailorable section (a list of dict items)."""
    return isinstance(value, list) and all(isinstance(i, dict) for i in value)


def _reorder_by_index(items: list, order: list) -> list | None:
    """Return ``items`` reordered by ``order`` (a permutation of its indices).

    Returns ``None`` if ``order`` is not a valid permutation of
    ``range(len(items))`` (so the caller skips a malformed reorder rather than
    dropping/duplicating items).
    """
    if not isinstance(order, list):
        return None
    try:
        idxs = [int(i) for i in order]
    except (TypeError, ValueError):
        return None
    if sorted(idxs) != list(range(len(items))):
        return None
    return [items[i] for i in idxs]


def _apply_reorder_sections(cv: dict, order: list) -> bool:
    """Reorder the top-level section keys per ``order`` (in place on ``cv``).

    ``order`` is a sequence of existing top-level keys. Protected keys keep
    their position; only tailorable section keys are reordered. Returns True if
    a reordering was applied.
    """
    if not isinstance(order, list):
        return False
    section_keys = [k for k in cv.keys() if k not in _PROTECTED_KEYS and _is_section_list(cv[k])]
    requested = [k for k in order if k in section_keys]
    # Must be a permutation of exactly the tailorable sections to be safe.
    if sorted(requested) != sorted(section_keys) or requested == section_keys:
        return False
    # Rebuild the dict preserving non-section keys in place and substituting
    # the section keys in their new order at the positions they occupied.
    new: dict[str, Any] = {}
    section_iter = iter(requested)
    for key in cv.keys():
        if key in section_keys:
            nk = next(section_iter)
            new[nk] = cv[nk]
        else:
            new[key] = cv[key]
    cv.clear()
    cv.update(new)
    return True


def _get_section(cv: dict, target: str) -> list | None:
    """Return the tailorable section list at ``target``, or None."""
    if not isinstance(target, str) or target in _PROTECTED_KEYS:
        return None
    value = cv.get(target)
    return value if _is_section_list(value) else None


def _before_matches(current: object, sug: dict) -> bool:
    """Require a verbatim source anchor for content-changing operations."""
    before = sug.get("before")
    return isinstance(before, str) and bool(before.strip()) and before == str(current)


def _apply_one(cv: dict, sug: dict) -> bool:
    """Apply a single suggestion to ``cv`` in place. True iff something changed.

    Defensive: any shape mismatch (bad index, wrong type, missing field) makes
    the op a no-op (returns False) so a malformed suggestion can't corrupt the
    document. The original facts are never extended here — content ops only
    move or replace existing fields with the model's ``after`` value, which is
    cross-referenced for fabrication downstream.
    """
    if not isinstance(sug, dict):
        return False
    op = sug.get("op")
    target = sug.get("target")

    if op == "reorder_sections":
        return _apply_reorder_sections(cv, sug.get("order"))

    if op in ("reorder_items", "reorder_bullets", "reorder_skill_items",
              "rewrite_bullet", "add_emphasis", "retag_stack"):
        section = _get_section(cv, target)
        if section is None:
            return False

        if op == "reorder_items":
            reordered = _reorder_by_index(section, sug.get("order"))
            if reordered is None:
                return False
            cv[target] = reordered
            return True

        # The remaining ops address one item by index.
        index = sug.get("index")
        if not isinstance(index, int) or not (0 <= index < len(section)):
            return False
        item = section[index]

        if op == "reorder_bullets":
            bullets = item.get("bullets")
            if not isinstance(bullets, list):
                return False
            reordered = _reorder_by_index(bullets, sug.get("order"))
            if reordered is None:
                return False
            item["bullets"] = reordered
            return True

        if op == "rewrite_bullet":
            bullets = item.get("bullets")
            b_idx = sug.get("bullet_index")
            after = sug.get("after")
            if (
                not isinstance(bullets, list)
                or not isinstance(b_idx, int)
                or not (0 <= b_idx < len(bullets))
                or not isinstance(after, str)
                or not after.strip()
            ):
                return False
            if not _before_matches(bullets[b_idx], sug):
                return False
            if bullets[b_idx] == after:
                return False
            bullets[b_idx] = after
            return True

        if op == "add_emphasis":
            # `after` is the new full value of a field on the item. Apply it to
            # the field named by `field` (default: the item's `items` for a
            # skills row, else `stack`). Never invents a key the item lacks.
            after = sug.get("after")
            field = sug.get("field")
            if not isinstance(after, str) or not after.strip():
                return False
            if not isinstance(field, str) or field not in item:
                # Infer a sensible target field if not specified.
                field = "items" if "items" in item else ("stack" if "stack" in item else None)
            if field is None or field not in item or not isinstance(item.get(field), str):
                return False
            if not _before_matches(item.get(field), sug):
                return False
            if item[field] == after:
                return False
            item[field] = after
            return True

        if op == "reorder_skill_items":
            items_str = item.get("items")
            order = sug.get("order")
            after = sug.get("after")
            # Prefer an explicit `after` string; else apply a comma-token reorder.
            if isinstance(after, str) and after.strip():
                if not _before_matches(item.get("items"), sug):
                    return False
                if item.get("items") == after:
                    return False
                item["items"] = after
                return True
            if isinstance(items_str, str) and isinstance(order, list):
                if not _before_matches(items_str, sug):
                    return False
                tokens = [t.strip() for t in items_str.split(",") if t.strip()]
                reordered = _reorder_by_index(tokens, order)
                if reordered is None:
                    return False
                item["items"] = ", ".join(reordered)
                return True
            return False

        if op == "retag_stack":
            after = sug.get("after")
            if not isinstance(after, str) or not after.strip():
                return False
            if "stack" not in item or item.get("stack") == after:
                return False
            if not _before_matches(item.get("stack"), sug):
                return False
            item["stack"] = after
            return True

    return False


def apply_suggestions(
    cv: dict[str, Any],
    suggestions: list[dict[str, Any]],
    *,
    summary: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str], list[str]]:
    """Apply tailoring ``suggestions`` to a COPY of ``cv`` (immutable on input).

    Args:
        cv: the canonical source CV model. NOT mutated — a deep copy is
            transformed and returned.
        suggestions: the LLM's list of typed operations. Malformed / unknown /
            out-of-range ops are skipped (recorded in the returned ``skipped``).
        summary: accepted for backward-compatible callers but no longer mutates
            the CV. Summary rewrites need a source-anchored suggestion path.

    Returns:
        ``(new_cv, applied_ids, skipped)`` — the tailored model, the ids of
        applied suggestions, and a list of ``"<id>: <reason>"`` for skipped
        ones. Every applied content change still flows through the
        anti-fabrication cross-reference and the 1-page gate downstream.
    """
    new_cv = copy.deepcopy(cv or {})
    applied: list[str] = []
    skipped: list[str] = []

    for sug in suggestions or []:
        sug_id = (sug.get("id") if isinstance(sug, dict) else None) or "(unnamed)"
        try:
            changed = _apply_one(new_cv, sug)
        except Exception as exc:  # noqa: BLE001 - one bad op never breaks the rest
            skipped.append(f"{sug_id}: error {type(exc).__name__}")
            continue
        if changed:
            applied.append(sug_id)
        else:
            skipped.append(f"{sug_id}: not applicable")

    return new_cv, applied, skipped
