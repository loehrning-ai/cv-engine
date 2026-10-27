"""Output language for the printed CV: the words the engine adds itself.

The CV text (roles, bullets, dates such as ``heute``) is written by the
author in whatever language they want. The engine only adds a handful of
words of its own: section headings, the connector in a date range, the
document title and ``<html lang>``. Those come from this table, selected by
the optional top-level ``lang`` field in ``cv.yaml``::

    lang: de        # "Berufserfahrung", "04/2024 bis heute", <html lang="de">

``lang`` is optional and defaults to ``en``, so every existing CV renders
byte-for-byte as before. To add a language, add one entry to ``LABELS``
with every key the English entry has (``tests/test_lang_labels.py`` checks
that).
"""
from __future__ import annotations

from types import MappingProxyType
from typing import Any, Mapping

DEFAULT_LANG = "en"

_EN = MappingProxyType({
    "date_connector": "to",
    "document_title": "{name} — CV",
    "publication_fallback": "Publication",
    "sections": MappingProxyType({
        "experience": "Experience",
        "education": "Education",
        "skills": "Skills",
        "projects": "Projects",
        "leadership": "Leadership",
        "others": "Other",
    }),
})

_DE = MappingProxyType({
    "date_connector": "bis",
    "document_title": "Lebenslauf {name}",
    "publication_fallback": "Veröffentlichung",
    "sections": MappingProxyType({
        "experience": "Berufserfahrung",
        "education": "Ausbildung",
        "skills": "Kenntnisse",
        "projects": "Projekte",
        "leadership": "Führung und Engagement",
        "others": "Sonstiges",
    }),
})

LABELS: Mapping[str, Mapping[str, Any]] = MappingProxyType({"en": _EN, "de": _DE})
SUPPORTED_LANGS: tuple[str, ...] = tuple(LABELS)


def _code(value: Any) -> str:
    """``"DE"``, ``" de-DE "`` and ``"de_CH"`` all become ``"de"``."""
    return str(value).strip().lower().replace("_", "-").split("-")[0]


def normalize_lang(value: Any) -> str:
    """Validate a ``lang`` value from the YAML; raise on an unsupported one.

    Missing or empty means the default. Used by ``content.load`` so a typo
    such as ``lang: deu`` fails the build with a clear message instead of
    silently printing English headings.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return DEFAULT_LANG
    if not isinstance(value, str):
        raise ValueError(
            f"lang={value!r} not supported. Use one of {SUPPORTED_LANGS}."
        )
    code = _code(value)
    if code not in LABELS:
        raise ValueError(
            f"lang={value!r} not supported. Use one of {SUPPORTED_LANGS}."
        )
    return code


def resolve_lang(value: Any) -> str:
    """Lenient twin of :func:`normalize_lang` for the renderer.

    The renderer can receive a model that never went through strict
    validation (editor previews of a half-edited file), so an unknown value
    falls back to the default instead of raising.
    """
    try:
        return normalize_lang(value)
    except ValueError:
        return DEFAULT_LANG


def labels_for(lang: Any) -> Mapping[str, Any]:
    return LABELS[resolve_lang(lang)]


def section_label(lang: Any, key: str, fallback: str) -> str:
    """Heading for a registered section; ``fallback`` for unknown keys."""
    return labels_for(lang)["sections"].get(key, fallback)
