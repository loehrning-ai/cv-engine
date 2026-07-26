"""Log redaction (Stage 4) — never let an API key reach the logs.

A stray ``logger.info("calling with key %s", key)`` or an exception whose
message embeds a key would otherwise persist a secret in plaintext. This
filter sits on the root logger and masks anything that looks like an
Anthropic / generic ``sk-`` key in the *rendered* log message before a
handler emits it — defence-in-depth on top of "never log keys deliberately".

It's a :class:`logging.Filter` (not a handler) so it applies uniformly to
every handler attached to the logger it's installed on.
"""
from __future__ import annotations

import logging
import re
import traceback

# Provider keys come in hyphenated families: Anthropic ``sk-ant-...``, OpenAI
# project/service keys ``sk-proj-...`` / ``sk-svcacct-...``, and the generic
# ``sk-...`` form. The generic pattern allows hyphens in the body so a
# multi-segment key like ``sk-proj-AbC-123...`` is caught whole (the old
# ``[A-Za-z0-9]+`` body stopped at the first hyphen and left such keys
# unredacted). We still require ≥20 trailing key chars so ordinary prose
# ("sk-" as a word) isn't clobbered. The ``ant``/``proj`` specific patterns run
# first so their mask is applied even though the generic one would also match.
#
# Google AI keys do NOT use the ``sk-`` family (Stage 4 — the Gemini provider):
#   * a Gemini Developer API key is ``AIza`` + exactly 35 url-safe chars;
#   * an OAuth2 access token is ``ya29.`` + a long url-safe body.
# These run alongside the ``sk-`` patterns so a stray Google key in a log line
# or exception message is masked just like an Anthropic key.
_PATTERNS = (
    re.compile(r"\b(?:authorization\s*[:=]\s*)?bearer\s+[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE),
    re.compile(r"\bcookie\s*:\s*[^\r\n]+", re.IGNORECASE),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\b(?:li_at|JSESSIONID|session_token|sid|connect\.sid)\s*=\s*[^;\s,&]+", re.IGNORECASE),
    re.compile(r"\b(?:access_token|refresh_token|id_token|jwt|session_token)['\"]?\s*[:=]\s*['\"]?[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE),
    re.compile(r"\b(?:api[_-]?key|token|secret|client[_-]?secret|password)['\"]?\s*[:=]\s*['\"]?[A-Za-z0-9._~+/=-]{16,}", re.IGNORECASE),
    re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    re.compile(r"sk-or-v1-[A-Za-z0-9_-]{20,}"),
    re.compile(r"sk-proj-[A-Za-z0-9_-]{20,}"),
    re.compile(r"sk-svcacct-[A-Za-z0-9_-]{20,}"),
    re.compile(r"sk-[A-Za-z0-9][A-Za-z0-9_-]{19,}"),
    re.compile(r"gsk_[A-Za-z0-9_-]{20,}"),
    # Google AI Studio / Gemini Developer API key (AIza + 35 chars).
    re.compile(r"AIza[0-9A-Za-z_-]{35}"),
    # Google OAuth2 access token (ya29.<body>).
    re.compile(r"ya29\.[0-9A-Za-z_-]+"),
)
_MASK = "sk-***REDACTED***"


def redact(text: str) -> str:
    """Return ``text`` with any key-shaped substring masked."""
    for pat in _PATTERNS:
        text = pat.sub(_MASK, text)
    return text


class KeyRedactionFilter(logging.Filter):
    """Mask key-shaped substrings in a record's message and args.

    We rewrite ``record.msg`` to the fully-rendered, redacted string and clear
    ``record.args`` so downstream ``%`` formatting can't re-introduce an
    unredacted arg. Returns ``True`` so the (now-masked) record still emits.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - never let logging crash on redaction
            return True
        redacted = redact(message)
        record.msg = redacted
        record.args = None
        if record.exc_info:
            record.exc_text = redact("".join(traceback.format_exception(*record.exc_info)))
            record.exc_info = None
        return True


def _has_redactor(filters: list) -> "KeyRedactionFilter | None":
    """Return an existing :class:`KeyRedactionFilter` in ``filters``, if any."""
    for existing in filters:
        if isinstance(existing, KeyRedactionFilter):
            return existing
    return None


def _known_loggers() -> list[logging.Logger]:
    """Return currently registered concrete loggers."""
    out: list[logging.Logger] = []
    for item in logging.Logger.manager.loggerDict.values():
        if isinstance(item, logging.Logger):
            out.append(item)
    return out


def _attach_to_handlers(logger: logging.Logger, flt: KeyRedactionFilter) -> None:
    for handler in list(logger.handlers):
        if _has_redactor(handler.filters) is None:
            handler.addFilter(flt)


def install(logger: logging.Logger | None = None) -> KeyRedactionFilter:
    """Install the redaction filter so it masks records from CHILD loggers too.

    A ``logging.Filter`` attached to a *logger* only runs for records logged
    DIRECTLY to that logger — Python's propagation walks ancestor *handlers*
    but NOT their filters. So a filter on the root logger alone would miss a
    record emitted by ``logging.getLogger("anything.child")`` that merely
    propagates up to the root's handlers.

    To actually cover child loggers we attach the SAME filter instance at the
    HANDLER level on every handler of the target logger (handler-level filters
    DO run on propagated records), in addition to the logger itself. We also
    install a one-time monkey-patch of ``Logger.addHandler`` on the root so any
    handler added LATER (e.g. by ``logging.basicConfig``) inherits the filter.

    Idempotent: re-installing never stacks duplicate filters.
    """
    target = logger or logging.getLogger()
    flt = _has_redactor(target.filters) or KeyRedactionFilter()
    if _has_redactor(target.filters) is None:
        target.addFilter(flt)

    # Cover propagated child records and pre-existing non-root child handlers:
    # filter at the handler level too.
    _attach_to_handlers(target, flt)
    if target is logging.getLogger():
        for known in _known_loggers():
            _attach_to_handlers(known, flt)

    # Ensure handlers added after install() still get the filter (root only, so
    # we patch once). A guard flag prevents re-patching on repeat installs.
    if target is logging.getLogger() and not getattr(
        logging.Logger, "_onepager_redact_patched", False
    ):
        _orig_add_handler = logging.Logger.addHandler

        def _patched_add_handler(self, hdlr):  # type: ignore[no-untyped-def]
            _orig_add_handler(self, hdlr)
            # Attach the active redactor (if installed on root) to new handlers.
            root = logging.getLogger()
            active = _has_redactor(root.filters)
            if active is not None and _has_redactor(hdlr.filters) is None:
                hdlr.addFilter(active)

        logging.Logger.addHandler = _patched_add_handler  # type: ignore[assignment]
        logging.Logger._onepager_redact_patched = True  # type: ignore[attr-defined]

    return flt
