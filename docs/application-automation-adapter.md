# Application Automation Adapter

Plan 103 introduces a backend-only external apply boundary. The product can
classify an application URL, prove a reviewed packet is ready, return a manual
open URL, and run fixture-only dry-runs. It does not submit applications and it
does not automate live job sites.

## Runtime Policy

External apply actions are fail-closed:

- `ONEPAGER_EXTERNAL_APPLY_ENABLED` defaults off.
- `ONEPAGER_EXTERNAL_APPLY_KILL_SWITCH` defaults active. Set it to `0` only for
  a controlled test or explicitly approved deployment.
- Both `ONEPAGER_EXTERNAL_APPLY_ENABLED=1` and
  `ONEPAGER_EXTERNAL_APPLY_KILL_SWITCH=0` are required before action routes run.
- `ONEPAGER_EXTERNAL_APPLY_SAFE_FIXTURE_HOSTS` is a comma-separated exact host
  allowlist for dry-run fixtures. No host is trusted by default.

## Host Classification

`tools/editor/apply_service.py` classifies targets without network access.

- LinkedIn hosts are always `manual_open` only. No dry-run, submit, session
  reuse, crawling, scraping, or browser control is allowed.
- Unknown hosts are always `manual_open` only.
- Recognized ATS hosts are still `manual_open` only unless a future approved
  adapter is built and separately gated.
- Dry-run is allowed only when the URL host exactly matches
  `ONEPAGER_EXTERNAL_APPLY_SAFE_FIXTURE_HOSTS`.
- Submit is always false in this architecture.

## Readiness Gates

`GET /api/jobs/<job_id>/apply/readiness` reads the existing job detail shape:

- the saved job must have an application URL,
- the full job description must be present,
- an application packet must exist,
- the packet must have `review_status=reviewed`,
- the packet must include both `tailored_cv_id` and `cover_letter_id`,
- those documents must still be linked under the job detail response.

The readiness response also returns the classifier result, effective config
state, required confirmations, and supported actions.

## Action Routes

`POST /api/jobs/<job_id>/apply/open`

- Requires the feature flag, inactive kill switch, readiness, and explicit
  confirmation.
- Records an attempt row.
- Returns the URL for a manual user-open action.
- Does not open a browser, fetch the URL, or mark the job as submitted.

`POST /api/jobs/<job_id>/apply/dry-run`

- Has the same gates as manual open.
- Additionally requires a safe fixture host.
- Accepts optional `fixture_fields` in the request body and returns a sanitized
  field report.
- Does not fetch the target URL.

Required confirmation payload:

```json
{
  "confirm": true,
  "confirmations": {
    "reviewed_packet_confirmed": true,
    "profile_answers_complete": true,
    "legal_answers_confirmed": true,
    "visa_answers_confirmed": true
  }
}
```

## Attempt Log Contract

Action routes insert into `application_apply_attempts` through
`auth.user_supabase().table(...).insert(...).execute()`. This keeps the code
compatible with the existing in-memory `FakeSupabase` tests and with an RLS
table in production.

Expected row fields:

- `job_id`
- `packet_id`
- `action`
- `status`
- `reason_code`
- `target_url` with query and fragment removed
- `target_host`
- `target_classification`
- `submitted=false`
- `automation_performed=false`
- `readiness`
- `confirmations`
- `metadata`
- `created_at`
- `updated_at`

If the log insert fails, the action fails closed.

## Forbidden Behavior

The adapter layer must not vendor or import third-party application-agent code,
reuse live-session cookies, store browser sessions, control a live browser,
fetch live application pages, bypass CAPTCHAs, answer legal questions
automatically, or submit forms.
