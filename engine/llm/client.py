"""Provider-agnostic LLM client (Stage 4).

The engine used to instantiate ``anthropic.Anthropic()`` directly at three
sites, each reading ``os.environ["ANTHROPIC_API_KEY"]``. That hardcodes both
the provider and the "operator's key from the environment" assumption — the
opposite of what a BYO-key, multi-provider SaaS needs.

This module introduces a tiny seam:

    make_client(provider, api_key) -> LLMClient
    LLMClient.complete(system, user, model, max_tokens) -> str

``complete`` is the one operation the CV use-cases need: a single-shot
system+user → text completion (with a prompt-cached system prompt). Tool use,
streaming, vision, etc. are intentionally out of scope here — add them when a
use-case needs them.

:class:`AnthropicClient` and :class:`GeminiClient` are wired (plan 002 stage 4 —
BYO key per provider + a provider/model selector, default Claude). OpenAI is an
explicit NotImplemented stub marking exactly where the next provider plugs in —
the interface is provider-agnostic so wiring one is additive, never a rewrite.

DEV-ONLY demo mode: when the ``ONEPAGER_DEMO_MODE`` env flag is truthy,
:func:`make_client` returns a deterministic, offline :class:`StubLLMClient`
instead of a real provider client (and the gateway bypasses the key/credit
requirement). This lets AI features be explored locally with NO API key. The
flag defaults OFF, so production behaviour is byte-identical to before.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from urllib.parse import urlparse

try:
    from demo_policy import demo_mode_enabled
except ImportError:  # pragma: no cover - package import outside normal entrypoints
    from engine.demo_policy import demo_mode_enabled

# Default model: the existing "cheapest smart tier" the editor shipped with.
# Stage 4 only parameterizes the KEY, not the model choice — keep this string
# byte-identical to the prior ai_extract default so behaviour is unchanged.
DEFAULT_MODEL = "claude-sonnet-4-5"

# Anthropic model allowlist surfaced in the provider/model selector. Claude stays
# the global default; these are the tiers the editor exposes.
ANTHROPIC_MODELS = (
    DEFAULT_MODEL,
    "claude-haiku-4-5",
    "claude-opus-4-5",
)

# --- Gemini (plan 002 stage 4) -------------------------------------------------
# The Gemini provider's default + allowlist. ``gemini-2.5-flash`` is stable and
# free-tier-eligible → the friendliest BYO first-run. Google rotates these IDs
# aggressively (2.0 retired June 2026), so they are CENTRALIZED here: a model
# retirement is a one-line edit, never a 404 hunt across the codebase.
GEMINI_DEFAULT_MODEL = "gemini-2.5-flash"
GEMINI_MODELS = (
    "gemini-2.5-flash",
    "gemini-3.5-flash",
    "gemini-2.5-pro",
    "gemini-2.5-flash-lite",
)
# Gemini's cheapest tier — the Haiku-equivalent slot used for the tailor call
# (and the key-validation probe) when no explicit model is requested.
GEMINI_TAILOR_MODEL = "gemini-2.5-flash-lite"

OPENAI_COMPATIBLE_DEFAULT_MODEL = "openai-compatible-default"
OLLAMA_DEFAULT_MODEL = os.environ.get("ONEPAGER_OLLAMA_MODEL", "llama3.1")


@dataclass(frozen=True)
class ProviderSpec:
    """Registry metadata for one LLM provider.

    The registry is the single policy surface for routing, validation, model
    defaults, managed-credit eligibility, and base URL handling. Provider IDs
    are deliberately boring strings because they are stored in profiles and
    `user_llm_keys`.
    """

    id: str
    label: str
    aliases: tuple[str, ...] = ()
    auth_type: str = "api_key"
    default_model: str = DEFAULT_MODEL
    models: tuple[str, ...] = ()
    managed_credit_eligible: bool = False
    hosted_base_url: str | None = None
    env_base_url: str | None = None
    base_url_policy: str = "hosted"
    validation: str = "none"
    redaction_family: str = "generic"

    def resolved_models(self) -> tuple[str, ...]:
        env_name = f"ONEPAGER_{self.id.upper()}_MODELS"
        configured = tuple(
            item.strip()
            for item in (os.environ.get(env_name) or "").split(",")
            if item.strip()
        )
        if configured:
            return configured
        return self.models or (self.default_model,)

    def resolved_base_url(self) -> str | None:
        if self.env_base_url:
            configured = os.environ.get(self.env_base_url)
            if configured:
                return configured.rstrip("/")
        return self.hosted_base_url.rstrip("/") if self.hosted_base_url else None


PROVIDER_REGISTRY: dict[str, ProviderSpec] = {
    "anthropic": ProviderSpec(
        id="anthropic",
        label="Claude (Anthropic)",
        default_model=DEFAULT_MODEL,
        models=ANTHROPIC_MODELS,
        managed_credit_eligible=True,
        validation="anthropic",
        redaction_family="anthropic",
    ),
    "gemini": ProviderSpec(
        id="gemini",
        label="Gemini (Google)",
        aliases=("google",),
        default_model=GEMINI_DEFAULT_MODEL,
        models=GEMINI_MODELS,
        validation="gemini",
        redaction_family="google",
    ),
    "ollama": ProviderSpec(
        id="ollama",
        label="Ollama (local)",
        auth_type="none",
        default_model=OLLAMA_DEFAULT_MODEL,
        models=(OLLAMA_DEFAULT_MODEL,),
        env_base_url="ONEPAGER_OLLAMA_BASE_URL",
        base_url_policy="local_only",
        validation="ollama",
    ),
    "openrouter": ProviderSpec(
        id="openrouter",
        label="OpenRouter",
        default_model=os.environ.get("ONEPAGER_OPENROUTER_MODEL", OPENAI_COMPATIBLE_DEFAULT_MODEL),
        models=(os.environ.get("ONEPAGER_OPENROUTER_MODEL", OPENAI_COMPATIBLE_DEFAULT_MODEL),),
        hosted_base_url="https://openrouter.ai/api/v1",
        validation="openai_compatible",
        redaction_family="openai_compatible",
    ),
    "deepseek": ProviderSpec(
        id="deepseek",
        label="DeepSeek",
        default_model=os.environ.get("ONEPAGER_DEEPSEEK_MODEL", OPENAI_COMPATIBLE_DEFAULT_MODEL),
        models=(os.environ.get("ONEPAGER_DEEPSEEK_MODEL", OPENAI_COMPATIBLE_DEFAULT_MODEL),),
        hosted_base_url="https://api.deepseek.com",
        validation="openai_compatible",
        redaction_family="openai_compatible",
    ),
    "groq": ProviderSpec(
        id="groq",
        label="Groq",
        default_model=os.environ.get("ONEPAGER_GROQ_MODEL", OPENAI_COMPATIBLE_DEFAULT_MODEL),
        models=(os.environ.get("ONEPAGER_GROQ_MODEL", OPENAI_COMPATIBLE_DEFAULT_MODEL),),
        hosted_base_url="https://api.groq.com/openai/v1",
        validation="openai_compatible",
        redaction_family="openai_compatible",
    ),
    "mistral": ProviderSpec(
        id="mistral",
        label="Mistral",
        default_model=os.environ.get("ONEPAGER_MISTRAL_MODEL", OPENAI_COMPATIBLE_DEFAULT_MODEL),
        models=(os.environ.get("ONEPAGER_MISTRAL_MODEL", OPENAI_COMPATIBLE_DEFAULT_MODEL),),
        hosted_base_url="https://api.mistral.ai/v1",
        validation="openai_compatible",
        redaction_family="openai_compatible",
    ),
    "openai_compatible": ProviderSpec(
        id="openai_compatible",
        label="OpenAI-compatible",
        default_model=os.environ.get("ONEPAGER_OPENAI_COMPATIBLE_MODEL", OPENAI_COMPATIBLE_DEFAULT_MODEL),
        models=(os.environ.get("ONEPAGER_OPENAI_COMPATIBLE_MODEL", OPENAI_COMPATIBLE_DEFAULT_MODEL),),
        env_base_url="ONEPAGER_OPENAI_COMPATIBLE_BASE_URL",
        base_url_policy="public_https",
        validation="openai_compatible",
        redaction_family="openai_compatible",
    ),
}

_ALIASES = {
    alias: provider_id
    for provider_id, spec in PROVIDER_REGISTRY.items()
    for alias in spec.aliases
}

# The canonical providers the editor exposes a selector for. Single source of
# truth — the gateway, account service, and UI all read from this.
SUPPORTED_PROVIDERS = tuple(PROVIDER_REGISTRY.keys())


def models_for(provider: str | None) -> tuple[str, ...]:
    """The allowlisted model ids for ``provider`` (canonicalized)."""
    spec = PROVIDER_REGISTRY.get(canonical_provider(provider))
    return spec.resolved_models() if spec else ()


def discover_ollama_models(base_url: str | None = None) -> tuple[str, ...]:
    """Return locally available Ollama models, or ``()`` when unavailable.

    Discovery is best-effort and local-only. A stopped Ollama daemon must not
    make `/api/models` fail, and a configured remote URL must still be rejected
    by the same local-only validator the runtime client uses.
    """
    url = validate_provider_base_url("ollama", base_url or "http://127.0.0.1:11434")
    req = urllib.request.Request(f"{url}/api/tags", headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=1.5) as resp:  # noqa: S310 - local-only URL
            parsed = json.loads(resp.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, ProviderConfigError):
        return ()
    models = parsed.get("models") if isinstance(parsed, dict) else None
    if not isinstance(models, list):
        return ()
    out: list[str] = []
    for item in models:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("model") or "").strip()
        if name:
            out.append(name)
    return tuple(dict.fromkeys(out))


def ollama_health(base_url: str | None = None) -> dict[str, object]:
    """Best-effort local Ollama health metadata for account/status UI."""
    url = validate_provider_base_url("ollama", base_url or "http://127.0.0.1:11434")
    req = urllib.request.Request(f"{url}/api/version", headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=1.5) as resp:  # noqa: S310 - local-only URL
            parsed = json.loads(resp.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, ProviderConfigError) as exc:
        return {"available": False, "error": type(exc).__name__}
    version = parsed.get("version") if isinstance(parsed, dict) else None
    return {"available": True, "version": str(version or "")}


def models_for_display(provider: str | None) -> tuple[str, ...]:
    """Model ids for UI display, including safe local discovery where supported."""
    p = canonical_provider(provider)
    configured = models_for(p)
    if p != "ollama":
        return configured
    discovered = discover_ollama_models()
    if not discovered:
        return configured
    return tuple(dict.fromkeys((*discovered, *configured)))


def default_model_for(provider: str | None) -> str:
    """The default model id for ``provider`` (canonicalized)."""
    spec = PROVIDER_REGISTRY.get(canonical_provider(provider))
    return spec.default_model if spec else DEFAULT_MODEL


def canonical_provider(provider: str | None) -> str:
    """Normalize a provider string to its canonical form.

    ``google`` is an accepted input alias for ``gemini`` (the SDK + Google's
    branding both use "Google AI"), but ``gemini`` is the single canonical value
    we store + route on, so a user never ends up with split vault rows.
    """
    p = (provider or "anthropic").strip().lower().replace("-", "_")
    return _ALIASES.get(p, p)


def resolve_model(provider: str | None, requested: str | None) -> str:
    """Return a valid model id for ``provider``, falling back to its default.

    An unknown or CROSS-PROVIDER model string (e.g. a Claude id requested under
    Gemini) is rejected and replaced by the provider's safe default — this is
    what neutralizes the "fast-rotating Gemini id → SDK 404" footgun: a bad id
    can never reach the SDK.
    """
    p = canonical_provider(provider)
    allowed = models_for(p)
    if requested and requested in allowed:
        return requested
    return default_model_for(p)


class LLMError(RuntimeError):
    """Raised when an LLM call can't be made or fails irrecoverably."""


class UnsupportedProviderError(LLMError):
    """Raised by the factory for a provider that isn't wired yet."""


def provider_spec(provider: str | None) -> ProviderSpec:
    p = canonical_provider(provider)
    try:
        return PROVIDER_REGISTRY[p]
    except KeyError as exc:
        raise UnsupportedProviderError(f"Unknown LLM provider {provider!r}.") from exc


class ProviderConfigError(LLMError):
    """Raised when a provider's configured base URL is unsafe or missing."""


def validate_provider_base_url(provider: str | None, base_url: str | None = None) -> str | None:
    """Validate and normalize the base URL for ``provider``.

    Hosted providers must use their registry base URL. Custom OpenAI-compatible
    endpoints must be public HTTPS and resolve only to public IPs. Ollama is the
    opposite: it may only target loopback/local-host URLs.
    """
    spec = provider_spec(provider)
    url = (base_url or spec.resolved_base_url() or "").strip().rstrip("/")
    if not url:
        if spec.base_url_policy == "hosted":
            return None
        raise ProviderConfigError(f"{spec.id} base URL is not configured.")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ProviderConfigError(f"{spec.id} base URL is invalid.")

    policy = spec.base_url_policy
    if policy == "hosted":
        hosted = spec.hosted_base_url.rstrip("/") if spec.hosted_base_url else ""
        if hosted and url != hosted:
            raise ProviderConfigError(f"{spec.id} base URL must use the hosted allowlist.")
        return url

    if policy == "local_only":
        host = parsed.hostname.lower()
        if parsed.scheme != "http":
            raise ProviderConfigError("Ollama must use a local http URL.")
        if host in {"localhost"} or host.startswith("127.") or host == "::1":
            return url
        raise ProviderConfigError("Ollama base URL must be local-only.")

    if policy == "public_https":
        if parsed.scheme != "https":
            raise ProviderConfigError("Custom OpenAI-compatible base URL must use https.")
        _assert_public_hostname(parsed.hostname)
        return url

    raise ProviderConfigError(f"Unknown base URL policy for {spec.id}.")


def _resolve_public_addresses(hostname: str) -> list[str]:
    """Resolve ``hostname`` ONCE and return its validated public IPs.

    A bare IP literal is checked in place and returned as the single address.
    Otherwise EVERY resolved address must be globally routable — one private /
    loopback / link-local answer vetoes the whole hostname. The returned
    addresses are the ONLY ones a request may dial: the caller pins the
    connection to one of them, so the IP validated here is the IP actually
    connected to (no second DNS lookup → no DNS-rebinding TOCTOU window).
    """
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None:
        if not literal.is_global:
            raise ProviderConfigError("Provider base URL resolves to a non-public address.")
        return [hostname]
    try:
        infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ProviderConfigError("Provider base URL hostname could not be resolved.") from exc
    addresses: list[str] = []
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if not addr.is_global:
            raise ProviderConfigError("Provider base URL resolves to a non-public address.")
        addresses.append(info[4][0])
    if not addresses:
        raise ProviderConfigError("Provider base URL hostname could not be resolved.")
    return list(dict.fromkeys(addresses))


def _assert_public_hostname(hostname: str) -> None:
    """Fail closed if ``hostname`` resolves to private/internal addresses."""
    _resolve_public_addresses(hostname)

# Sentinel "key" handed to the stub on the demo-mode path. It is never sent to
# any provider (the stub ignores it); it only keeps the call sites' "non-empty
# key" contract satisfied so nothing 503s for a blank key in demo mode.
DEMO_STUB_API_KEY = "onepager-demo-mode"


@runtime_checkable
class LLMClient(Protocol):
    """Minimal provider-agnostic completion surface.

    A client wraps one provider + one API key. ``complete`` performs a single
    system+user → text turn. ``system`` is passed in a prompt-cache-friendly
    way by concrete implementations (the system prompt is the stable prefix).
    """

    def complete(
        self,
        *,
        system: str,
        user: str,
        model: str | None = None,
        max_tokens: int = 1024,
        cache_system: bool = True,
    ) -> str:
        """Return the assistant's text for a single system+user exchange."""
        ...


class AnthropicClient:
    """:class:`LLMClient` backed by the Anthropic Python SDK.

    Holds the (per-request) API key — either the user's decrypted BYO key or,
    on the managed-credit path, the operator key. The SDK is imported lazily
    so the server boots even when ``anthropic`` isn't installed and no AI
    route is hit.
    """

    provider = "anthropic"

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise LLMError("AnthropicClient requires a non-empty API key.")
        self._api_key = api_key

    def _sdk(self):
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - import guard
            raise LLMError("anthropic SDK is not installed.") from exc
        return anthropic.Anthropic(api_key=self._api_key)

    def complete(
        self,
        *,
        system: str,
        user: str,
        model: str | None = None,
        max_tokens: int = 1024,
        cache_system: bool = True,
    ) -> str:
        client = self._sdk()
        # Wrap the system prompt in a cache_control:ephemeral block so a
        # static system prefix is ~90% cheaper on repeated calls. When the
        # system prompt is short/dynamic (polish prompts) the caller can opt
        # out and we send a plain string.
        if cache_system:
            system_arg = [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        else:
            system_arg = system
        response = client.messages.create(
            model=model or DEFAULT_MODEL,
            max_tokens=max_tokens,
            system=system_arg,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(
            b.text for b in response.content if getattr(b, "type", None) == "text"
        ).strip()


class GeminiClient:
    """:class:`LLMClient` backed by Google's unified ``google-genai`` SDK.

    Holds one per-request API key (the user's decrypted BYO key — managed
    credits are Anthropic-only, see the gateway). The SDK is imported lazily so
    the server boots even when ``google-genai`` isn't installed and no Gemini
    route is hit, exactly like :class:`AnthropicClient`.

    The request shape differs from Anthropic: the system prompt is
    ``config.system_instruction`` (text-only — fine, our prompts are all text),
    the user turn is a plain ``contents`` string, and the output is the
    ``response.text`` convenience accessor.

    ``cache_system`` is accepted for protocol parity but IGNORED: Gemini caching
    is a separate, stateful, TTL'd context-cache resource rather than a per-call
    flag, and Gemini's implicit caching already discounts repeated prefixes with
    no code. Treat the parameter's intent (cheap repeated prefixes) as honoured
    automatically.
    """

    provider = "gemini"

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise LLMError("GeminiClient requires a non-empty API key.")
        self._api_key = api_key

    def _sdk(self):
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:  # pragma: no cover - import guard
            raise LLMError("google-genai SDK is not installed.") from exc
        # Pass api_key EXPLICITLY (never rely on the env): the SDK auto-reads
        # GOOGLE_API_KEY / GEMINI_API_KEY, and GOOGLE_API_KEY takes precedence —
        # an ambient env key would silently shadow a user's BYO key. The timeout
        # unit is MILLISECONDS (not seconds) so a hung call can't pin a worker.
        return genai.Client(
            api_key=self._api_key,
            http_options=types.HttpOptions(timeout=60_000),  # ms
        )

    def complete(
        self,
        *,
        system: str,
        user: str,
        model: str | None = None,
        max_tokens: int = 1024,
        cache_system: bool = True,
    ) -> str:
        from google.genai import types

        client = self._sdk()
        response = client.models.generate_content(
            model=model or GEMINI_DEFAULT_MODEL,
            contents=user,
            config=types.GenerateContentConfig(
                system_instruction=system,
                max_output_tokens=max_tokens,
            ),
        )
        # response.text can be None (safety-blocked or truncated mid-thought) —
        # coalesce to "" so downstream guards see an empty string and fail
        # gracefully rather than crashing on None.
        return (response.text or "").strip()


class _AuthStrippingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Drop the BYO ``Authorization`` header on cross-host redirects.

    ``urllib`` otherwise re-sends the original request's headers to the
    redirect target — a malicious or compromised endpoint could 302 to a
    different host and exfiltrate the user's bearer key. Same-host redirects
    keep the header so legitimate in-origin hops still authenticate.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req is None:
            return None
        old_host = urlparse(req.full_url).netloc.lower()
        new_host = urlparse(new_req.full_url).netloc.lower()
        if new_host != old_host:
            new_req.remove_header("Authorization")
        return new_req


# Module-level opener so every OpenAI-compatible request gets the redirect
# policy above (urlopen() with a Request builds a default opener otherwise).
_SECURE_OPENER = urllib.request.build_opener(_AuthStrippingRedirectHandler())


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection that dials validated/pinned IPs, never re-resolving.

    ``http.client`` normally resolves ``self.host`` at CONNECT time — a second
    DNS lookup that reopens the DNS-rebinding window after the base URL was
    validated. Here the TCP connection targets the pinned, already-validated
    addresses instead (same technique as ``tools/editor/ssrf.py``), while TLS
    SNI and certificate hostname verification keep using the ORIGINAL hostname
    (``server_hostname=self.host``) — the TOCTOU is closed WITHOUT weakening
    certificate checks.
    """

    def __init__(self, *args, _pinned_addresses=None, **kwargs):
        self._pinned_addresses = _pinned_addresses or {}
        super().__init__(*args, **kwargs)

    def connect(self):  # noqa: D401 - mirrors http.client internals
        pins = self._pinned_addresses.get((self.host or "").lower())
        if not pins or self._tunnel_host:
            # No pin for this host (a proxy tunnel or a cross-host redirect
            # hop): standard resolve-at-connect behavior. The BYO
            # Authorization header is never attached on those hops.
            super().connect()
            return
        last_error: OSError | None = None
        for pin in pins:
            sock = None
            try:
                sock = self._create_connection((pin, self.port), self.timeout, self.source_address)
                self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
                return
            except OSError as exc:
                last_error = exc
                if sock is not None:
                    sock.close()
        if last_error is not None:
            raise last_error


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    """HTTPS handler that builds :class:`_PinnedHTTPSConnection` instances."""

    def __init__(self, pinned_addresses: dict[str, list[str]]) -> None:
        super().__init__(context=ssl.create_default_context())
        self._pinned_addresses = pinned_addresses

    def https_open(self, req):  # noqa: D102 - mirrors urllib internals
        pins = self._pinned_addresses

        def _connection_factory(host, *, timeout=None, **kwargs):
            return _PinnedHTTPSConnection(host, timeout=timeout, _pinned_addresses=pins, **kwargs)

        return self.do_open(_connection_factory, req, context=self._context)


def _pinned_https_opener(host: str, pins: list[str]):
    """Build an opener that dials the validated ``pins`` for ``host``.

    The redirect policy matches ``_SECURE_OPENER`` (cross-host redirects strip
    the BYO Authorization header) with a FRESH handler instance: an
    OpenerDirector re-parents its handlers, so the module-level opener's
    handler must never be shared across openers.
    """
    handler = _PinnedHTTPSHandler({host.lower(): list(pins)})
    return urllib.request.build_opener(_AuthStrippingRedirectHandler(), handler)


class OpenAICompatibleClient:
    """OpenAI-compatible chat-completions client.

    This intentionally uses the standard library instead of importing an SDK
    that may auto-discover ambient environment keys. The caller passes the
    decrypted BYO key explicitly; the provider registry supplies and validates
    the base URL. A custom (``public_https`` policy) base URL is additionally
    pinned to its validated IPs at request time — see :meth:`_secure_opener`.
    """

    def __init__(self, provider: str, api_key: str, *, base_url: str | None = None) -> None:
        self.provider = canonical_provider(provider)
        if not api_key:
            raise LLMError(f"{self.provider} requires a non-empty API key.")
        self._api_key = api_key
        self._base_url = validate_provider_base_url(self.provider, base_url)
        if not self._base_url:
            raise ProviderConfigError(f"{self.provider} base URL is not configured.")
        self._pin_custom_base_url = self._is_custom_public_base_url()

    def _is_custom_public_base_url(self) -> bool:
        """True when the base URL is a user-supplied public HTTPS endpoint.

        Only the ``public_https`` policy (custom OpenAI-compatible endpoint)
        gets IP pinning: hosted providers keep their fixed allowlisted URLs on
        the shared opener, and unknown provider ids (only reachable in tests
        with a patched validator) keep the previous behavior.
        """
        try:
            return provider_spec(self.provider).base_url_policy == "public_https"
        except UnsupportedProviderError:
            return False

    def _secure_opener(self):
        """Return the opener for this request, pinning custom base URLs.

        A custom base-URL hostname is resolved ONCE here, every resolved
        address is (re-)validated as public, and the TLS connection dials one
        of THOSE addresses while SNI + certificate hostname verification keep
        using the original hostname. The IP checked is the IP dialed — a DNS
        rebind between config-time validation and connect cannot redirect the
        BYO Authorization header to an internal host. An IP-literal base URL
        involves no DNS at all, and hosted providers use fixed allowlisted
        URLs, so both keep the shared opener.
        """
        if not self._pin_custom_base_url:
            return _SECURE_OPENER
        parsed = urlparse(self._base_url or "")
        host = parsed.hostname or ""
        if parsed.scheme != "https" or not host:
            return _SECURE_OPENER  # the policy enforces https; defensive
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass  # a hostname: resolve + validate + pin below
        else:
            return _SECURE_OPENER  # IP literal: no DNS, no rebinding window
        pins = _resolve_public_addresses(host)
        return _pinned_https_opener(host, pins)

    def complete(
        self,
        *,
        system: str,
        user: str,
        model: str | None = None,
        max_tokens: int = 1024,
        cache_system: bool = True,
    ) -> str:
        payload = {
            "model": resolve_model(self.provider, model),
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self._base_url}/chat/completions",
            data=data,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        # DNS-rebinding hardening happens here: for a custom base URL this
        # resolves once and pins the connection to a validated address.
        opener = self._secure_opener()
        try:
            with opener.open(req, timeout=60) as resp:  # noqa: S310 - registry-validated, IP-pinned URL
                parsed = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise LLMError(f"{self.provider} request failed.") from exc
        try:
            content = parsed["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"{self.provider} returned an invalid response.") from exc
        return str(content or "").strip()


class OllamaClient:
    """Local Ollama chat client with no stored secret."""

    provider = "ollama"

    def __init__(self, api_key: str | None = None, *, base_url: str | None = None) -> None:
        self._base_url = validate_provider_base_url("ollama", base_url or "http://127.0.0.1:11434")

    def complete(
        self,
        *,
        system: str,
        user: str,
        model: str | None = None,
        max_tokens: int = 1024,
        cache_system: bool = True,
    ) -> str:
        payload = {
            "model": resolve_model("ollama", model),
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": {"num_predict": max_tokens},
        }
        req = urllib.request.Request(
            f"{self._base_url}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 - local-only URL
                parsed = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise LLMError("Ollama request failed.") from exc
        message = parsed.get("message") if isinstance(parsed, dict) else None
        if not isinstance(message, dict):
            raise LLMError("Ollama returned an invalid response.")
        return str(message.get("content") or "").strip()


class StubLLMClient:
    """DEV-ONLY deterministic :class:`LLMClient` for ``ONEPAGER_DEMO_MODE``.

    It makes NO network call and needs NO API key. Its :meth:`complete` looks
    at the system prompt to decide which use-case it is serving and returns a
    fixed, **CV-grounded** transform of the inputs so the real anti-fabrication,
    cliché, and 1-page guards downstream still PASS:

      * cover-letter generation → a short, plain letter built ONLY from facts
        already present in the supplied CV/JD (candidate name + most-recent
        role/company from the CV, target company/role from the JD). No invented
        employers, numbers, or credentials; no cliché reject-phrases; well under
        the word cap. Survives ``cross_reference_facts`` + the cliché scan.
      * CV tailoring → a no-fabrication, no-op suggestion set
        (``{"suggestions": []}``) so the tailored copy equals the source CV:
        it passes Guard A (every token grounded) and the 1-page gate (an
        unchanged 1-page CV stays one page).
      * extract → minimal valid CV YAML inferred from the pasted text (a name
        line + one experience entry), never fabricating beyond the input.
      * polish (bullet / section) → the input returned essentially unchanged
        (the documented "already tight → return as-is" behaviour). No
        fabrication.

    Correctness of the guards passing outranks the demo marker: the marker is
    only added where it cannot trip a guard (e.g. an HTML/JSON comment), and is
    dropped entirely from the cover-letter prose, whose every proper noun must
    be grounded.

    Only used when :func:`demo_mode_enabled` is True; never in production.
    """

    provider = "stub"

    def __init__(self, api_key: str | None = None) -> None:
        # The key is accepted for interface parity but intentionally ignored.
        self._api_key = api_key or ""

    def complete(
        self,
        *,
        system: str,
        user: str,
        model: str | None = None,
        max_tokens: int = 1024,
        cache_system: bool = True,
    ) -> str:
        sys_l = (system or "").lower()
        if "you write cover letters" in sys_l:
            return _stub_cover_letter(user)
        if "you are an ats rubric" in sys_l:
            return _stub_ats_rubric(user)
        if "cv tailoring engine" in sys_l:
            return _stub_tailor_json()
        if "cv/resume data extractor" in sys_l:
            return _stub_extract_yaml(user)
        # Polish (bullet/section) and any other short transform: echo the input
        # back unchanged — the prompts explicitly allow "return it unchanged",
        # which is the safest no-fabrication transform for a stub.
        return (user or "").strip()


# ---------------------------------------------------------------------------
# Stub transforms — pure functions, fully deterministic, grounded in inputs.
# ---------------------------------------------------------------------------
_TAG_RE_TMPL = r"<{tag}>\s*(.*?)\s*</{tag}>"


def _extract_tagged_json(user: str, tag: str) -> dict:
    """Pull the JSON object embedded in ``<tag>…</tag>`` from a user message.

    The cover-letter user template wraps the CV JSON in ``<cv_json>`` and the
    parsed JD JSON in ``<job_description>``. Returns ``{}`` if absent/malformed
    — the stub degrades to a thinner (still grounded) letter rather than ever
    fabricating.
    """
    m = re.search(_TAG_RE_TMPL.format(tag=re.escape(tag)), user or "", re.DOTALL)
    if not m:
        return {}
    try:
        parsed = json.loads(m.group(1))
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _first_experience(cv: dict) -> dict:
    exp = cv.get("experience") if isinstance(cv, dict) else None
    if isinstance(exp, list) and exp and isinstance(exp[0], dict):
        return exp[0]
    return {}


def _stub_cover_letter(user: str) -> str:
    """Build a deterministic, grounded cover letter from the prompt's inputs.

    SAFETY (this is the whole point of the stub): the cover-letter grounding
    guard (``cross_reference_facts``) rejects ANY un-grounded capitalised token,
    un-grounded number, OR run of 2+ consecutive lowercase words that are
    neither stop-words, "common prose" words, nor grounded in the CV/JD. So the
    letter is assembled from a tiny, audited vocabulary:

      * proper nouns: ONLY the candidate name + most-recent role/company (from
        the CV JSON) and the target role/company (from the JD JSON);
      * connective prose: ONLY whitelisted stop-words and the guard's
        "common prose" set (built / shipped / delivered / owned / work / role /
        team / data / systems / pipeline / engineer / welcome / chance / …);
      * NO numbers at all (a metric absent from the CV would trip the guard);
      * NO cliché reject-phrase.

    Every sentence below was verified to pass ``cross_reference_facts`` against a
    representative CV+JD. Sentences are only included when their facts exist, so
    a thin CV yields a short (still honest) letter, never a padded fiction.
    Comfortably under the ~400-word cap.
    """
    cv = _extract_tagged_json(user, "cv_json")
    jd = _extract_tagged_json(user, "job_description")
    exp = _first_experience(cv)

    cur_role = str(exp.get("role") or "").strip()
    cur_company = str(exp.get("company") or "").strip()
    target_role = str(jd.get("title") or "").strip()
    target_company = str(jd.get("company") or "").strip()

    sentences: list[str] = []

    # Opening — anchor to the candidate's current role/company (both grounded).
    if cur_role and cur_company:
        sentences.append(f"I work as a {cur_role} at {cur_company}")
    elif cur_company:
        sentences.append(f"I have done my recent work at {cur_company}")
    elif cur_role:
        sentences.append(f"I work as a {cur_role}")
    else:
        sentences.append("I would bring solid, hands-on work to this role")

    # Substance — grounded, number-free framing of real work (only prose/stop
    # words plus grounded nouns). No invented metrics.
    sentences.append("I have built and shipped data systems and pipelines")
    if cur_company:
        sentences.append(f"At {cur_company} I owned work across the stack")

    # Tie to the target role/company (grounded from the JD).
    if target_role and target_company:
        sentences.append(
            f"The {target_role} role at {target_company} would value my work"
        )
    elif target_company:
        sentences.append(f"This role at {target_company} would value my work")
    elif target_role:
        sentences.append(f"The {target_role} role would value my work")

    # Close — forward-looking, specific, no filler-only paragraph.
    if target_company:
        sentences.append(
            f"I would welcome the chance to help the team at {target_company}"
        )
    else:
        sentences.append("I would welcome the chance to help your team")

    body = ". ".join(s.strip() for s in sentences if s.strip()) + "."
    # NOTE: no demo marker is embedded in the prose — a non-grounded marker
    # token would risk an anti-fabrication flag, and guard-compliance wins.
    return body


def _stub_ats_rubric(user: str) -> str:
    """Return a deterministic, grounded ATS rubric JSON for demo mode.

    Mirrors the schema the real rubric prompt asks for (four categories +
    strengths / gaps / suggestions + bonus / deductions) so the validator parses
    it cleanly. Every evidence string is quoted from the CV/JD actually embedded
    in the user message (Global G9: no fabrication) — a thin CV simply yields
    empty evidence and neutral scores. No network, no key; used only when
    :func:`demo_mode_enabled` is True.
    """
    cv = _extract_tagged_json(user, "cv_json")
    jd = _extract_tagged_json(user, "job_description")
    exp = _first_experience(cv)

    cur_role = str(exp.get("role") or "").strip()
    cur_company = str(exp.get("company") or "").strip()
    target_role = str(jd.get("title") or "").strip()

    # Production evidence is grounded in the candidate's most-recent role line.
    prod_evidence: list[str] = []
    if cur_role and cur_company:
        prod_evidence.append(f"{cur_role} at {cur_company}")
    elif cur_company:
        prod_evidence.append(cur_company)

    strengths: list[str] = []
    if prod_evidence:
        strengths.append(prod_evidence[0])

    # A neutral, honest, reframe-only suggestion grounded in an existing role.
    suggestions: list[str] = []
    if cur_company:
        suggestions.append(f"quantify your impact at {cur_company}")

    gaps: list[str] = []  # demo stub asserts no specific gap (avoids fabrication)

    def _cat(score: int, evidence: list[str]) -> dict:
        return {"score": score, "max": 100, "evidence": evidence, "reasoning": "demo mode"}

    payload = {
        "score": 50,  # the real validator recomputes this from category weights
        "categories": {
            "open_source": _cat(40, []),
            "self_projects": _cat(40, []),
            "production": _cat(60 if prod_evidence else 40, prod_evidence),
            "technical_skills": _cat(50, []),
        },
        "strengths": strengths,
        "gaps": gaps,
        "suggestions": suggestions,
        "bonus": 0,
        "deductions": 0,
        "_note": f"demo mode: offline ATS rubric stub (target role: {target_role or 'n/a'})",
    }
    return json.dumps(payload)


def _stub_tailor_json() -> str:
    """Return a no-op, no-fabrication tailoring result.

    An empty suggestion list means the deterministic applier returns the source
    CV unchanged, so Guard A (anti-fabrication cross-reference) and Guard B (the
    1-page gate) both pass for any already-valid 1-page CV. A leading JSON
    comment-style marker is avoided (strict JSON has no comments); the demo
    nature is conveyed by the inert ``warnings`` note, which the route surfaces
    but no guard inspects.
    """
    return json.dumps(
        {
            "fit": {"score": None, "verdict": "demo mode", "target_role": ""},
            "keywords": {"present": [], "missing": [], "inferable": []},
            "suggestions": [],
            "summary": {"recommended": False, "text": "", "rationale": ""},
            "warnings": ["demo mode: tailoring stub made no changes"],
        }
    )


def _stub_extract_yaml(user: str) -> str:
    """Infer a minimal valid CV YAML from pasted resume text (no fabrication).

    Uses the first non-empty line as the name and emits one placeholder-free
    experience entry so the result is schema-valid without inventing employers,
    dates, or metrics the text doesn't contain. The leading YAML comment marks
    it as demo output (comments are ignored by the YAML loader, so this is
    inert for every downstream consumer).
    """
    name = "Your Name"
    for line in (user or "").splitlines():
        stripped = line.strip()
        if stripped:
            name = stripped[:80]
            break
    # Escape any stray double-quote so the single YAML string stays valid.
    safe_name = name.replace('"', "'")
    return (
        "# demo mode — generated offline by the CV Copilot LLM stub\n"
        f'name: "{safe_name}"\n'
        "accent: \"#111111\"\n"
        "font: serif\n"
        "contact:\n"
        "  - label: Edit me\n"
        "experience:\n"
        "  - role: Your Role\n"
        "    company: Your Company\n"
        "    location: City, Country\n"
        "    start: '2024'\n"
        "    end: Present\n"
        "    bullets:\n"
        "      - Replace this with a real achievement from your background.\n"
    )


def make_client(provider: str, api_key: str) -> LLMClient:
    """Build an :class:`LLMClient` for ``provider`` using ``api_key``.

    Anthropic and Gemini are wired; everything else raises
    :class:`UnsupportedProviderError`. This is the single branch point a new
    provider is added to (alongside a new client class above).

    DEV-ONLY: when :func:`demo_mode_enabled` is True the provider/key are
    ignored and a deterministic offline :class:`StubLLMClient` is returned, so
    every AI feature works locally with no key — for EVERY provider, so the
    selector stays visible while the stub answers. Defaults OFF (production path
    unchanged).
    """
    if demo_mode_enabled():
        return StubLLMClient(api_key)
    spec = provider_spec(provider)
    p = spec.id
    if p == "anthropic":
        return AnthropicClient(api_key)
    if p == "gemini":
        return GeminiClient(api_key)
    if p == "ollama":
        return OllamaClient(api_key)
    if spec.validation == "openai_compatible":
        return OpenAICompatibleClient(p, api_key)
    raise UnsupportedProviderError(f"Unknown LLM provider {provider!r}.")
