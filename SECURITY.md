# Security Policy

## Reporting a vulnerability

Do not open a public issue, pull request, or discussion for a suspected vulnerability.

Send a private report to `tim@loehrning.ai`. Include the affected route or component, impact, reproduction steps, and a minimal proof of concept. Remove credentials, personal data, and production records before sending evidence.

If GitHub private vulnerability reporting is enabled, the repository Security tab is an equivalent private channel.

## Scope

Supported code is the current default branch after publication. The following surfaces are in scope:

- **The bring-your-own-key vault.** `tools/editor/vault.py` performs AES-256-GCM envelope encryption: a per-key data key encrypts the provider API key, the platform key encrypts the data key, and the owner's user id is authenticated as additional data on both layers so a stolen row cannot be replayed under a different user. Plaintext key material exists in-process only immediately before an outbound provider call. It is never persisted, never logged, and never returned by an API response; the stored record exposes only the last four characters. Any path that violates that is in scope.
- **Tenant isolation.** Row-level security policies in `supabase/migrations/` are the isolation boundary between accounts. A query, policy gap, or service-role path that lets one tenant read or write another tenant's rows is in scope.
- **Server-side request forgery.** The editor fetches from URLs that a user can influence: custom `openai_compatible` provider base URLs, the Ollama base URL, and job postings gated by `ONEPAGER_JOB_URL_ALLOWLIST`. `validate_provider_base_url` in `engine/llm/client.py` requires hosted providers to match their registry URL, requires custom OpenAI-compatible endpoints to be public HTTPS resolving to public addresses, and restricts Ollama to loopback. A bypass of any of those constraints, including redirect and DNS-rebinding paths, is in scope.
- **Untrusted document parsing.** Uploaded PDF and DOCX files are parsed for text extraction. Memory exhaustion, path traversal from archive members, or code execution reachable from a crafted document is in scope.
- **YAML loading.** `yaml.safe_load` is used on every parsing path. A route that reaches a non-safe loader, or any deserialization primitive that constructs arbitrary objects from user input, is in scope.
- **Rendered CV output.** Inline markdown in CV text is escaped and link targets are filtered before reaching the HTML and PDF renderers. Injection that survives that path is in scope.
- **Container boundary.** The application and source-built Caddy images use digest-pinned Wolfi runtimes, fixed nonroot users, read-only root filesystems, dropped capabilities, and `no-new-privileges`. A path that escapes those restrictions, introduces a critical/high image CVE, falsifies the recorded Caddy/Go module graph, or restores a root runtime is in scope.

Out of scope:

- **Self-host misconfiguration.** A missing or weak `ONEPAGER_KEK`, an editor bound to a public interface instead of loopback, a database exposed without its policies applied, or a deployment without TLS is a configuration defect on the operator's side.
- **Loopback-only demo behavior.** Demo mode deliberately relaxes authentication so the editor can be exercised without a provisioned backend. The application refuses startup when `PUBLIC_BASE_URL` names a public origin and rejects demo requests unless both the HTTP Host and direct peer are loopback. A bypass of either control is in scope. Findings that require already-local access to a correctly loopback-bound demo instance are not.
- **Provider-side handling.** What a third-party model provider does with prompt content, and how it retains or trains on that content, is governed by that provider's terms rather than by this repository. Reports about which provider to trust belong in the support process.

General product questions and factual documentation corrections belong in the support process.

Acknowledgement is targeted within five business days. Fix and disclosure timing depends on severity and reproducibility.

Container scanning is a release-time control, not a claim that an image can
never become vulnerable. CI and deployment reject any critical or high finding
known to Trivy's current vulnerability database at scan time. The scanner image
is pinned by digest, local images are supplied as read-only `docker save` tar
inputs without a Docker-socket mount or scanner account, and scanner/database
errors fail closed. New advisories require a new scan and, when affected, a
rebuilt image or dependency update before release.
