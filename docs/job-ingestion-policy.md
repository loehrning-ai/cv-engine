# Job Ingestion Policy

CV Copilot supports job ingestion only through compliant, auditable paths.

## LinkedIn

- No blind LinkedIn scraping.
- No stored LinkedIn cookies, browser sessions, or stealth automation.
- URL-only LinkedIn imports are rejected before any fetch.
- LinkedIn imports are allowed only when the user pastes the posting text, or when an operator configures an approved official/partner adapter with documented permission.
- Manual LinkedIn imports are marked `source_provider=linkedin`, `source_type=manual_paste`, and `verification_status=needs_review`.

## URL Fetching

- Pasted job descriptions are the default path.
- URL-only fetching is disabled unless `ONEPAGER_JOB_URL_ALLOWLIST` explicitly includes the source host.
- Allowed URL fetches still pass through SSRF protection, response-size caps, and per-source rate limits.

## Adapter Contract

Any third-party or partner adapter must define:

- source id, label, supported filters, and rate budget;
- terms basis and operator evidence for job-data retrieval;
- provenance fields including source provider, source type, external id, content hash, last seen, and verification state;
- no credential or cookie persistence unless a separate audited contract exists;
- RLS-safe import runs and preview results before save-selected writes to `saved_jobs`.
