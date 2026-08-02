# Deploying CV Copilot (operator runbook)

This is the **manual** production runbook. Nothing here runs automatically —
the GitHub Actions workflow (`.github/workflows/deploy.yml`) is a template gated
to manual dispatch, and the `caddy` reverse-proxy service only starts under the
`prod` compose profile. You (the operator) run these steps with your own
accounts (Hetzner, a domain, Supabase, Cloudflare R2, optionally PostHog).

The app and account-erasure worker use the same Docker image: gunicorn serves
the editor + API on `:8000`, the worker completes D+30 erasure, and Caddy
fronts the app with automatic TLS. Data location depends on the regions the
operator actually selects.

---

## 1. Provision a VPS (Hetzner Cloud)

1. Create a **CX22** (or larger) Hetzner Cloud server, image **Ubuntu 24.04**,
   in an **EU** location (e.g. Nuremberg/Falkenstein) for data residency.
2. Add your SSH key during creation.
3. SSH in and install Docker:
   ```bash
   curl -fsSL https://get.docker.com | sh
   ```
4. (Recommended) create a non-root `deploy` user in the `docker` group and a
   firewall allowing **22, 80, 443** only.

## 2. Point your domain

Create a DNS **A record** for your domain (e.g. `onepager.example.com`) → the
VPS public IP. Caddy will obtain a Let's Encrypt certificate automatically on
first boot once DNS resolves.

## 3. Configure Supabase

Create or select a Supabase project in the region required by your deployment
policy. In the Supabase dashboard:

- **Authentication → URL Configuration**: set the Site URL and add a redirect
  URL of `https://<your-domain>` (so magic-link / OAuth return to prod).
- **Authentication → Providers**: enable **Google** and **GitHub** and paste
  each provider's OAuth client id + secret (magic-link email works without
  this).
- **Authentication → Emails / SMTP**: configure a custom SMTP sender
  (**Resend**) for deliverable production magic-link emails.

## 4. Configure Cloudflare R2 (EU) — optional

For signed-URL PDF delivery: create an **EU** R2 bucket and an S3 API token,
then set `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`,
`R2_BUCKET`. Leave all four unset to use the in-process direct-stream fallback
(fully functional, no R2), and set `ONEPAGER_R2_NEVER_USED=1` as an explicit,
durable assertion that this deployment has never stored PDFs in R2. Once R2
has ever been enabled, retain working bucket credentials for erasure even if
new PDF uploads are later disabled. The worker refuses to hard-delete accounts
when it cannot prove that historical R2 objects are absent or deletable.

## 5. Set secrets / environment

The app reads configuration from `.env` on the VPS. Either create it by hand
(`cp .env.example .env` and fill it) or let the deploy workflow write it from
secrets in a protected GitHub **production environment**. Restrict that
environment to `main`, require an independent reviewer, prevent self-review,
and do not store production values as repository-level secrets. The deploy
workflow fails before transfer if any required secret is missing. Required
values:

| Variable | What it is |
|---|---|
| `PUBLIC_BASE_URL` | `https://<your-domain>` — SEO canonical/og + sitemap |
| `ONEPAGER_DOMAIN` | `<your-domain>` — Caddy TLS host |
| `ACME_EMAIL` | Let's Encrypt contact email |
| `SUPABASE_URL` | operator-selected Supabase project URL |
| `SUPABASE_PUBLISHABLE_KEY` | Supabase publishable (browser-safe) key |
| `SUPABASE_JWKS_URL` | Supabase JWKS endpoint (JWT verification) |
| `SUPABASE_SERVICE_ROLE_KEY` | service-role key (server-only; never browser) |
| `ANTHROPIC_API_KEY` | operator key funding the 3 free managed generations |
| `ONEPAGER_KEK` | platform key-encryption key (BYO-key AES-GCM envelope) |
| `ONEPAGER_REVIEW_TOKEN_SECRET` | signing secret for reviewed CV / packet apply tokens |
| `FLASK_SECRET_KEY` | fallback reviewed-apply token signing secret; keep distinct from primary review-token secret |
| `ONEPAGER_PRIVACY_CONTACT` | monitored data-controller email; public startup fails without it |
| `ONEPAGER_OLLAMA_BASE_URL` | optional local-only Ollama endpoint, defaults to `http://127.0.0.1:11434` |
| `ONEPAGER_OLLAMA_MODEL` | optional local Ollama default model |
| `ONEPAGER_OPENROUTER_MODEL` | optional default model for OpenRouter |
| `ONEPAGER_DEEPSEEK_MODEL` | optional default model for DeepSeek |
| `ONEPAGER_GROQ_MODEL` | optional default model for Groq |
| `ONEPAGER_MISTRAL_MODEL` | optional default model for Mistral |
| `ONEPAGER_OPENAI_COMPATIBLE_BASE_URL` | optional custom OpenAI-compatible public HTTPS endpoint |
| `ONEPAGER_OPENAI_COMPATIBLE_MODEL` | optional custom OpenAI-compatible default model |
| `ONEPAGER_<PROVIDER>_MODELS` | optional comma-separated model allowlist override where supported |
| `ONEPAGER_JOB_URL_ALLOWLIST` | optional comma-separated job URL source hosts; empty disables URL-only fetching |
| `ONEPAGER_JOB_URL_RATE_LIMIT_PER_MINUTE` | optional per-source URL import budget, defaults to `20` |
| `LINKEDIN_APPROVED_ADAPTER_ENABLED` / `LINKEDIN_PARTNER_CONFIG_ID` | optional approved LinkedIn adapter flags; default-disabled and not a blind scraper |
| `R2_*` | Cloudflare R2 creds (optional — see step 4) |
| `ONEPAGER_R2_NEVER_USED` | set to `1` only if this deployment has never used R2; mutually exclusive with `R2_*` |
| `ONEPAGER_ACCOUNT_PURGE_BATCH_SIZE` | optional erasure batch size, default `20` |
| `ONEPAGER_ACCOUNT_PURGE_LEASE_MINUTES` | optional claim lease, default `30`; keep above worst-case cleanup duration |
| `ONEPAGER_ACCOUNT_PURGE_POLL_SECONDS` | optional worker cadence, default `300` |
| `POSTHOG_KEY` | **optional** analytics; omit to keep analytics a no-op |
| `POSTHOG_HOST` | optional, defaults to `https://eu.posthog.com` |
| `ONEPAGER_ATS_RUBRIC` | **optional** kill-switch for the LLM ATS rubric; unset/truthy = on, falsey (`0`/`false`/`no`/`off`) forces the deterministic scorer for every request (see "The ATS score" below) |

> Never commit `.env`. It is gitignored. The deploy workflow writes it on the
> VPS from protected production-environment secrets and `chmod 600`s it.

Do not set ambient provider SDK keys such as `GOOGLE_API_KEY`,
`GEMINI_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `DEEPSEEK_API_KEY`,
`GROQ_API_KEY`, or `MISTRAL_API_KEY` in the server environment. User-provider
keys are stored in the encrypted vault and passed explicitly through the
registry. Ambient SDK fallback is treated as leakage risk.

Anthropic is the only operator-funded provider. Managed credits must not be
extended to Gemini, Ollama, OpenAI-compatible providers, or hosted compatible
vendors without a separate billing and audit contract.

## 5a. Apply Supabase migrations

Apply migrations in order. On a first deployment, where no old application can
receive traffic, run:

```bash
supabase db push
```

or apply every SQL file under `supabase/migrations/` with your migration runner.
For an existing deployment, use the maintenance sequence below before a push
that includes migration `20260801145140`. The current migration ledger is:

- `0001_init_schema.sql`
- `0002_credits.sql`
- `0003_pdf_cleanup.sql`
- `0004_account_deletion.sql`
- `0005_hardening.sql`
- `0006_provider_prefs.sql`
- `0007_catalog_metadata.sql`
- `0008_first_run_template.sql`
- `0009_provider_registry.sql`
- `0010_job_import_provenance.sql`
- `0011_document_last_used.sql`
- `0012_application_packets.sql`
- `0013_application_apply_attempts.sql`
- `0014_job_experience_requirements.sql`
- `0015_application_packet_readiness_lineage.sql`
- `0016_tailor_review_states.sql`
- `0017_job_import_trust_hardening.sql`
- `0018_job_import_rls_delete_hardening.sql`
- `0019_account_deletion_worker_claim.sql`
- `0020_data_api_privilege_hardening.sql`
- `0021_foreign_key_indexes.sql`
- `0022_fix_account_deletion_claim_conflict.sql`
- `0023_security_contract_hardening.sql`
- `0024_tenant_relationship_constraints.sql`
- `20260801145140_preserve_application_history_deletes.sql`
- `20260801160000_job_workflow_atomicity.sql`
- `20260801170000_application_packet_atomicity.sql`
- `20260801180500_evidence_backed_job_status.sql`
- `20260801180820_release_schema_contract_probe.sql`

Post-migration checks:

```bash
python3 -m pytest tests/test_stage2_auth.py tests/test_stage4_vault.py tests/test_stage7_jobs.py
python3 -m pytest tests/test_hardening_web.py tests/test_stage8_pdf_store.py
```

Migration `20260801145140` begins a forward-only application cutover. It
revokes legacy direct document/PDF writes and makes the matching server RPCs
and PDF cleanup worker mandatory. Migrations `20260801160000` and
`20260801170000` complete the boundary for imported-job creation, job metadata
merges, source revalidation, cover-review confirmation, and partial
packet-lineage merging. Migration `20260801180820` adds the read-only release
contract/ACL evidence RPC and refuses compatibility unless migration
`20260801180500`'s evidence-backed job-status trigger exists, is enabled, and
invokes the expected trigger function. The candidate image and all five dated
migrations are one release contract.

Execute an upgrade across that boundary in this order:

1. Require the exact candidate commit to pass CI, immutable image scanning,
   static/unit gates, and image build checks.
2. On the VPS, record the current immutable app and Caddy image IDs. Stop every
   old Caddy, app, account-purge worker, and PDF-cleanup worker container. Stop
   Caddy first. Verify every recorded container reports `false` for
   `{{.State.Running}}`.
3. Keep the entire previous release stopped. Apply the migrations and verify
   the live ledger with `supabase migration list --linked`; every file through
   `20260801180820_release_schema_contract_probe.sql` must be present.
4. Dispatch the protected workflow for the same commit with
   `forward_only_maintenance=true`. This input is an operator assertion that
   ingress and the old application were stopped before the schema change.
5. The workflow locates prior containers with `docker ps -aq` but drains every
   matching container without `--latest`. It probes the candidate before
   draining production, compares the exact schema-contract descriptor embedded
   in both immutable app images, then runs the previous image's compatibility
   probe before any candidate service or directory swap.

The schema probe performs one read-only OpenAPI `GET /rest/v1/`, then calls the
service-role-only, `STABLE` `release_schema_contract()` evidence RPC. It requires
exact RPC argument sets, every runtime-used relation column, worker contract
version 2, the enabled evidence-backed job-status trigger, and current
`authenticated` execute grants for browser-facing RPCs.
The evidence RPC reads only catalog privileges/version state and writes no
database row. If the workflow observes any old app, Caddy, purge worker, or PDF
cleanup worker still running at a forward-only boundary, it drains all of them
and fails; verify maintenance state and rerun the same release. After the
migration commits, recovery is forward-only unless the previous image has the
exact same descriptor and its probe passes against the current database.

Migration `0019` drops the legacy database-only
`purge_deleted_accounts(integer)` routine. Remove any old pg_cron or external
schedule that invokes that function. Database-only purge is prohibited because
it cannot delete R2 objects or persistent-volume photos.

Treat migration `0019` and the first worker deployment as one forward-only
rollout. Apply the migration immediately before dispatching the matching
release. If migration `0019` has reached Supabase but the safety candidate
cannot become healthy, the application swap is refused and the existing
application remains in an explicit erasure-blocked state until the migration,
Supabase, R2, network, or worker fault is corrected. A pre-`0019` application
image is schema-incompatible and must not resume after the migration. The
workflow starts and health-checks the matching worker image before the
application swap as the read-only `onepager-erasure-candidate`. A prior failed
release can also leave a `onepager-erasure-keeper`. A successful Compose-worker
check removes both standalone safety containers. Any failed post-swap recovery
stops both standalone workers with the rest of the release boundary.

New writable columns/tables require RLS, pending-erasure behavior, and
column-scoped grants. Do not replace these with blanket grants.

## 6. First deploy

The GitHub Actions deploy workflow ships the checked-out artifact over SSH and
writes `.env` from repo-secret environment variables on the VPS. It does not
clone the private repository from the VPS, and it excludes internal plan files,
agent state, reports, test/cache output, generated browser-test failures,
personal photos, `.env*`, credential/cert/key/token JSON patterns, and local CV
content from the transferred artifact. It extracts into
`/opt/onepager.next`, validates compose there, then swaps it into
`/opt/onepager` and keeps the previous tree at `/opt/onepager.previous`.

The application image is built from the digest-pinned Wolfi base in
`Dockerfile`. It installs Wolfi Python `3.13.14-r3`, pip `26.1.2-r1`, and
version-pinned Cairo/Pango/GDK-Pixbuf/Harfbuzz/font dependencies. Python
packages install from `requirements.lock` with `--require-hashes`. The
application runs as UID/GID `999:999`; this intentionally preserves ownership
compatibility with volumes created by the retired Debian image.

The proxy is not pulled from a prebuilt Caddy image. `Dockerfile.caddy` builds
Caddy `v2.11.4` with Go `1.26.5` from a digest-pinned builder, forces
`google.golang.org/grpc` to `v1.82.1`, and copies only the static binary,
license, CA bundle, and build metadata into a second digest-pinned Wolfi
runtime. It runs as UID/GID `65532:65532` with a read-only root filesystem.
The production Compose services all drop Linux capabilities, enable
`no-new-privileges`, use bounded PID limits, and mount no-exec temporary
filesystems. The Caddy runtime does not contain curl.

Both continuous CI and the manual deploy workflow build the two images, verify
the Python/SQLite/WeasyPrint runtime, validate the Caddy module and Go build
metadata, and start the nonroot/read-only proxy path. They then invoke
`tools/ci/scan_container_images.sh`, which pins Trivy to
`aquasec/trivy:0.72.0@sha256:cffe3f5161a47a6823fbd23d985795b3ed72a4c806da4c4df16266c02accdd6f`.
The script exports each local image with `docker save` and scans the resulting
tar through Trivy's read-only `--input /input/image.tar` interface. It does not
mount the Docker socket and requires no scanner account or vendor entitlement.
The immutable scanner downloads the current vulnerability database at runtime,
fails closed if the scan or database update cannot complete, and rejects any
critical or high finding known at scan time. CI retains the JSON and table
reports for 30 days.

The deploy workflow then rejects secret-like baked environment/history entries
and forbidden credential files, records the content-addressed image IDs and
archive SHA-256, and retains the image archive for one day. The VPS verifies and
loads that exact archive. Production Compose receives the direct image IDs
through `docker-compose.release.yml`. That production override removes both
build definitions and fails closed when either immutable image reference is
missing. The workflow also persists those references in the release `.env` and
runs with `--no-build --pull never`; the VPS Compose path cannot silently
rebuild or fall back to a mutable tag after the scan.

A zero result is a point-in-time release gate, not a permanent vulnerability
guarantee. Re-run the workflows for every release and after
vulnerability-database updates.

Inspect the source-build evidence in a built proxy image with:

```bash
docker run --rm --entrypoint /bin/busybox onepager-caddy:2.11.4 \
  cat /usr/share/onepager/caddy-build-modules.txt
docker run --rm --entrypoint /bin/busybox onepager-caddy:2.11.4 \
  cat /usr/share/onepager/caddy-build-info.txt
```

The CI audit toolchain installs from `requirements-audit.lock` with hashes.
Regenerate the Python locks with:

```bash
python3 -m piptools compile requirements.txt --generate-hashes --output-file requirements.lock
python3 -m piptools compile requirements-audit.in --generate-hashes --output-file requirements-audit.lock
```

**Manually on the VPS:**
```bash
set -euo pipefail
umask 077
mkdir -p /opt/onepager
# Copy or extract the checked-out release artifact into /opt/onepager.
cd /opt/onepager
cp .env.example .env   # then fill it in (step 5)
test -n "$(grep '^ONEPAGER_DOMAIN=' .env | cut -d= -f2-)"
test -n "$(grep '^ACME_EMAIL=' .env | cut -d= -f2-)"
test -n "$(grep '^ONEPAGER_PRIVACY_CONTACT=' .env | cut -d= -f2-)"
docker compose -p onepager --profile prod --env-file .env config --quiet
docker compose -p onepager --profile prod --env-file .env build app caddy
bash tools/ci/scan_container_images.sh
export ONEPAGER_APP_IMAGE="$(docker image inspect onepager:dev --format '{{.Id}}')"
export ONEPAGER_CADDY_IMAGE="$(docker image inspect onepager-caddy:2.11.4 --format '{{.Id}}')"
export COMPOSE_FILE="docker-compose.yml:docker-compose.release.yml"
printf '%s\n' \
  "ONEPAGER_APP_IMAGE=\"$ONEPAGER_APP_IMAGE\"" \
  "ONEPAGER_CADDY_IMAGE=\"$ONEPAGER_CADDY_IMAGE\"" \
  'COMPOSE_FILE="docker-compose.yml:docker-compose.release.yml"' \
  >> .env
schema_probe_env="$(mktemp /tmp/onepager-schema-probe.XXXXXX)"
trap 'rm -f "$schema_probe_env"' EXIT
schema_probe_url="$(
  python3 tools/ci/compose_env_to_docker.py --get .env SUPABASE_URL
)"
schema_probe_key="$(
  python3 tools/ci/compose_env_to_docker.py --get \
    .env SUPABASE_SERVICE_ROLE_KEY
)"
test -n "$schema_probe_url"
test -n "$schema_probe_key"
printf '%s=%s\n' \
  SUPABASE_URL "$schema_probe_url" \
  SUPABASE_SERVICE_ROLE_KEY "$schema_probe_key" \
  > "$schema_probe_env"
unset schema_probe_url schema_probe_key
docker run --rm --env-file "$schema_probe_env" \
  --user 999:999 --read-only --cap-drop ALL \
  --security-opt no-new-privileges --pids-limit 64 \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m \
  --entrypoint python3 \
  "$ONEPAGER_APP_IMAGE" \
  tools/editor/schema_compatibility_probe.py --probe
rm -f "$schema_probe_env"
trap - EXIT
# Required once when upgrading volumes written by the old root Caddy image;
# harmless on a new deployment.
for caddy_volume in onepager_caddy_data onepager_caddy_config; do
  docker volume create "$caddy_volume" >/dev/null
  docker run --rm \
    --user 0:0 \
    --read-only \
    --cap-drop ALL \
    --cap-add CHOWN \
    --security-opt no-new-privileges \
    --volume "$caddy_volume:/mnt" \
    --entrypoint /bin/busybox \
    onepager-caddy:2.11.4 \
    chown -R 65532:65532 /mnt
done
docker compose -p onepager --profile prod --env-file .env config --images
docker compose -p onepager --profile prod --env-file .env \
  up -d --no-build --pull never
docker compose -p onepager ps
```

The `prod` profile is incomplete unless `app`, `purge-worker`,
`pdf-cleanup-worker`, and `caddy` are all running and both workers are healthy.
The purge healthcheck executes a
non-mutating service-role RPC that exists only after migration `0019`. When R2
is configured, it also authenticates a one-key prefix listing against the
configured bucket. In never-used mode it verifies that no PDF artifact row
exists.

With R2 configured, the PDF cleanup healthcheck proves the exact two
authorities used by cleanup: it calls `DeleteObject` for a fresh,
guaranteed-absent key below the reserved
`_onepager_pdf_cleanup_healthcheck_/` prefix, then calls
`claim_pdf_artifact_cleanup` with fresh UUIDs and requires the authoritative
`missing` result. In `ONEPAGER_R2_NEVER_USED=1` mode, the same service is a
fail-closed no-op: startup, every cycle, and every healthcheck require both
`pdf_artifacts` and `pdf_artifact_cleanup_queue` to remain empty. Any durable
artifact evidence makes the service unhealthy instead of pretending cleanup is
available. It never selects, claims, or deletes queued tenant work during a
healthcheck. The R2 worker consumes only due rows through the token-bound claim
RPC, then acknowledges with the same claim token. A queued active generation
is removed from the outbox without deleting its object. A future `not_before`
preserves already-issued signed URLs and in-flight upload candidates.

**Or via GitHub Actions** (after configuring the protected `production`
environment and its secrets listed in `deploy.yml`): Actions tab → **deploy**
→ *Run workflow*. It SSHes in, writes
`.env` from secrets, loads the scan-passed image archive, validates the
build-free production Compose override, and reads the candidate image's schema
descriptor. Before draining or swapping the live directory, it performs the
candidate's non-mutating schema/ACL probe. It then compares and probes the
previous immutable app image and runs the PDF cleanup authority probe from the
scanned candidate image. With the explicit never-used assertion, that probe
instead proves both PDF artifact tables are empty without requiring R2
credentials. Schema-probe containers receive only `SUPABASE_URL`
and `SUPABASE_SERVICE_ROLE_KEY`; their entrypoint is forced to Python. It then
starts only the recorded immutable image IDs. Deployment fails if a preflight
or either worker healthcheck fails.

Automated restoration of the previous checkout and image IDs is enabled only
when its descriptor exactly equals the candidate descriptor and the previous
image passes the same database probe. Otherwise the workflow never retags or
restarts the previous image. An incompatible post-swap failure retains the
candidate checkout but stops every candidate app, Caddy, account-purge worker,
and PDF-cleanup worker container. Maintenance remains active for a corrected
forward release; failed candidate ingress is never exposed.

## 7. Verify

```bash
export COMPOSE_FILE="docker-compose.yml:docker-compose.release.yml"
curl -fsS https://<your-domain>/healthz        # {"ok": true}
curl -fsS https://<your-domain>/                # landing page HTML
curl -fsS https://<your-domain>/robots.txt      # crawl rules + sitemap line
curl -i   https://<your-domain>/api/cvs         # 401 without a bearer token
worker_id="$(docker compose -p onepager --profile prod ps -q purge-worker)"
test -n "$worker_id"
test "$(docker inspect --format '{{.State.Health.Status}}' "$worker_id")" = healthy
pdf_worker_id="$(docker compose -p onepager --profile prod ps -q pdf-cleanup-worker)"
test -n "$pdf_worker_id"
test "$(docker inspect --format '{{.State.Health.Status}}' "$pdf_worker_id")" = healthy
docker compose -p onepager logs --tail=20 purge-worker
docker compose -p onepager logs --tail=20 pdf-cleanup-worker
```

Worker logs must contain aggregate cycle counts only. They must not contain
owner UUIDs, claim tokens, R2 object keys, credentials, or exception messages.
An R2 or photo cleanup failure increments `deferred` and leaves the
Auth/database account intact for a later lease-based retry. After an Auth Admin
call, the worker verifies the profile cascade so a lost HTTP response is
resolved by database postcondition rather than by the response alone.

After any failed post-swap recovery, verify standalone erasure workers are
stopped with the application boundary:

```bash
test -z "$(docker ps -q --filter 'name=^/onepager-erasure-keeper$')"
test -z "$(docker ps -q --filter 'name=^/onepager-erasure-candidate$')"
```

Do not restart an old standalone worker after a forward-only boundary. Correct
the release and start the matching Compose workers through a forward deploy.

Open `https://<your-domain>/` → landing page → **Get started** → sign in →
the first-run tour walks through import / AI key / tailor / build.

Provider verification:

```bash
python3 -m pytest tests/test_provider_registry.py tests/test_stage4_gemini.py
python3 -m pytest tests/test_stage5_cover_letter.py tests/test_stage6_tailor.py tests/test_stage5_ats_service.py
```

Browser verification:

```bash
ONEPAGER_DEMO_MODE=true python3 tools/editor/server.py
ONEPAGER_E2E_RETRY=0 ONEPAGER_E2E_BASE_URL=http://127.0.0.1:5567 python3 -m pytest tests/e2e/test_layout_contracts.py tests/e2e/test_workflow_contracts.py
```

Demo mode is restricted to direct loopback access and must remain unset in the
deployment environment. A public `PUBLIC_BASE_URL` causes startup refusal.

Leak scan the exact `git archive` export before any public release. These
commands pin the scanner images used for the release gate: Gitleaks 8.30.1 and
TruffleHog 3.95.9. A finding, malformed report, or scanner failure is fatal.

```bash
export EXPORT_DIR=/absolute/path/to/fresh-export
test -d "$EXPORT_DIR"

docker run --rm -v "$EXPORT_DIR:/src:ro" \
  ghcr.io/gitleaks/gitleaks@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f \
  detect --source=/src --no-git --redact --exit-code=1

docker run --rm -v "$EXPORT_DIR:/src:ro" \
  trufflesecurity/trufflehog@sha256:59b244249d1a1aef4baa24fe73d3c931616264482580d806d77f6c74d26b3e42 \
  filesystem /src --no-update --fail >/dev/null

detect_report="$(mktemp /tmp/cv-detect-secrets.XXXXXX.json)"
uvx --from detect-secrets==1.5.0 \
  detect-secrets scan --all-files "$EXPORT_DIR" > "$detect_report"
test "$(jq '[.results[]?[]?] | length' "$detect_report")" = "0"
rm -f "$detect_report"
```

After initializing and committing the fresh one-root repository, scan its
complete history again:

```bash
docker run --rm -v "$EXPORT_DIR:/src:ro" \
  ghcr.io/gitleaks/gitleaks@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f \
  detect --source=/src --redact --exit-code=1

docker run --rm -v "$EXPORT_DIR:/src:ro" \
  trufflesecurity/trufflehog@sha256:59b244249d1a1aef4baa24fe73d3c931616264482580d806d77f6c74d26b3e42 \
  git file:///src --no-update --fail >/dev/null
```

Public repository release is stricter than the Docker artifact. Keep `.env`,
`content/cv.yaml`, `CLAUDE.md`, `AGENTS.md`, and `plans/` out of public source
releases.

## Rollback

The automated workflow captures app and Caddy image IDs from running or stopped
prior containers before loading a replacement. It restores those exact IDs only
when the previous app image exposes the same schema-contract descriptor as the
candidate and its read-only schema probe passes against the current database.
An image without the probe, with a different descriptor, or with a failing
probe is not rollback-compatible.

For a manual rollback, use the immutable IDs recorded for the candidate and
previous releases. Do not rebuild. Generate the Docker-safe environment, compare
the image-owned descriptors without network access, and run the previous image's
non-mutating probe before stopping the candidate:

```bash
set -euo pipefail
umask 077
test "${CURRENT_APP_IMAGE_ID:-}" != ""
test "${PREVIOUS_APP_IMAGE_ID:-}" != ""
test "${PREVIOUS_CADDY_IMAGE_ID:-}" != ""
cd /opt/onepager
schema_probe_env="$(mktemp /tmp/onepager-schema-probe.XXXXXX)"
trap 'rm -f "$schema_probe_env"' EXIT
schema_probe_url="$(
  python3 tools/ci/compose_env_to_docker.py --get .env SUPABASE_URL
)"
schema_probe_key="$(
  python3 tools/ci/compose_env_to_docker.py --get \
    .env SUPABASE_SERVICE_ROLE_KEY
)"
printf '%s=%s\n' \
  SUPABASE_URL "$schema_probe_url" \
  SUPABASE_SERVICE_ROLE_KEY "$schema_probe_key" \
  > "$schema_probe_env"
unset schema_probe_url schema_probe_key

current_contract="$(
  docker run --rm --network none --user 999:999 --read-only \
    --cap-drop ALL --security-opt no-new-privileges --entrypoint python3 \
    "$CURRENT_APP_IMAGE_ID" \
    tools/editor/schema_compatibility_probe.py --describe
)"
previous_contract="$(
  docker run --rm --network none --user 999:999 --read-only \
    --cap-drop ALL --security-opt no-new-privileges --entrypoint python3 \
    "$PREVIOUS_APP_IMAGE_ID" \
    tools/editor/schema_compatibility_probe.py --describe
)"
test "$previous_contract" = "$current_contract"
docker run --rm --env-file "$schema_probe_env" \
  --user 999:999 --read-only --cap-drop ALL \
  --security-opt no-new-privileges --entrypoint python3 \
  "$PREVIOUS_APP_IMAGE_ID" \
  tools/editor/schema_compatibility_probe.py --probe

docker compose -p onepager --profile prod --env-file .env \
  stop caddy app purge-worker pdf-cleanup-worker
cd /
rm -rf /opt/onepager.failed
mv /opt/onepager /opt/onepager.failed
mv /opt/onepager.previous /opt/onepager
cd /opt/onepager
export ONEPAGER_APP_IMAGE="$PREVIOUS_APP_IMAGE_ID"
export ONEPAGER_CADDY_IMAGE="$PREVIOUS_CADDY_IMAGE_ID"
docker compose -p onepager --profile prod --env-file .env \
  up -d --no-build --pull never --remove-orphans
curl -fsS https://<your-domain>/healthz
```

If descriptor comparison or the previous-image probe fails, do not execute the
directory moves or Compose commands. Do not restore the previous image. Keep
the previous app, Caddy, account-purge worker, and PDF-cleanup worker stopped.
Retain the candidate checkout and immutable images, correct the candidate/schema
fault, and deploy forward.

## The ATS score (LLM rubric → deterministic fallback)

`/tailor`'s ATS score is computed in two layers, **deterministic-first**, so it
**always** returns a score and never hard-fails:

- **Deterministic keyword scorer** (`engine/ats/score.py`) — pure, offline, no
  LLM. It is the sole source of truth when no AI key is available, the rubric
  flag is off, or the rubric call fails for any reason. It also feeds the rubric
  a keyword-coverage signal.
- **LLM rubric** (`engine/ats/rubric.py`) — a richer, job-relative analysis
  (four categories with grounded evidence, plus strengths / gaps / honest,
  reframe-only suggestions). It runs **only** when the rubric flag is on **and**
  an AI key resolves.

Operator-relevant behaviour:

- **BYO-key.** The rubric uses the same key resolution as the other AI features
  (`llm_gateway.resolve_llm`: a user's own encrypted key first, then the operator
  key funding the free managed generations). **ATS scoring is free** — when no
  key resolves it silently uses the deterministic score and returns **200, never
  a 402** (no credit is consumed on the fallback path).
- **Graceful fallback.** Every rubric failure mode — timeout, rate-limit (429),
  auth error, empty or malformed reply, unparseable JD — is caught and falls back
  to the deterministic score. The response carries `source: "rubric" |
  "deterministic"` and, on a fallback, a short `note` explaining why (key/provider
  specifics are never echoed).
- **Kill-switch.** Set `ONEPAGER_ATS_RUBRIC` to a falsey value (`0` / `false` /
  `no` / `off`) to force the deterministic scorer for **every** request — a
  one-flag rollback if the rubric ever misbehaves. Unset (or any truthy value)
  leaves it on. It is read live per request, so it can be toggled without a
  restart.
- **Observability.** Each ATS scoring call emits one structured `ats_score` log
  line (`source` / `provider` / `model` / `latency_ms` / `score`, plus a `reason`
  on a fallback). No key or CV content is logged; the root key-redaction filter
  (`logredact`) is a defence-in-depth backstop.
- **Anti-fabrication + injection hygiene.** Rubric suggestions are filtered
  against the CV's own facts before they reach the UI (a suggestion may only ask
  the user to reframe/surface/quantify existing work — see the grounding note
  below). The JD is untrusted text: `cover_letter.jd.parse_jd` caps its length
  and strips injection-prone control / invisible-format characters before it ever
  enters a prompt.

## Known limitations

### Anti-fabrication grounding is best-effort defense-in-depth

The AI tailor and cover-letter features run a deterministic post-generation
**grounding check** (`engine/cover_letter/guards.py:cross_reference_facts`):
every proper noun / number / employer in the generated text must trace back to
the user's own CV (or, for cover letters, the job description). An ungrounded
employer, skill, or metric blocks the save (`422 unverified_claims`) and nothing
is persisted.

This is a backstop, **not a guarantee**. Pure string-grounding cannot catch
*every* adversarial phrasing — in particular an all-lowercase or obfuscated
fabrication that mimics ordinary prose can still slip through (single lowercase
tokens are intentionally not all flagged, because doing so would block
legitimate CVs). The check is tuned to flag the high-signal cases (capitalized
proper nouns, comma-listed skills, multi-word lowercase runs, and a lone
lowercase org directly after `at`/`for`/`with`/`@`) while keeping false
positives near zero.

The **primary** protections are, in order:

1. **Prompt guardrails (G1–G6)** — the generation prompt instructs the model to
   use only facts present in the supplied CV/JD and never invent employers,
   titles, dates, or metrics.
2. **Owner-scoping** — a user can only generate against their **own** CV, and
   tailoring is *duplicate-then-edit*: the original CV is preserved
   byte-identical, so a fabrication can never corrupt the source of truth.
3. **User review** — the generated document is shown to the user before use;
   the human applying for the job is the final reviewer of their own claims.

Treat the grounding check as a safety net that catches the obvious cases, not as
a substitute for the prompt guardrails, owner-scoping, and user review above.

## Provider registry behavior

Every AI-consuming route resolves provider and model through the registry and
gateway. Invalid providers, invalid models, key/provider mismatches, and
disallowed base URLs fail explicitly with sanitized errors.

Ollama is local-only. Custom OpenAI-compatible endpoints must be public HTTPS.
Hosted compatible providers use fixed base URLs. The server must never use
ambient SDK env keys as tenant credentials.

## Job ingestion policy

Blind LinkedIn scraping is disabled. The allowed LinkedIn paths are:

1. user-provided posting content pasted into the app,
2. user-provided URL context paired with visible posting text,
3. official/partner access configured by the operator and disabled by default,
4. third-party adapters only after their terms explicitly permit the intended
   job-data retrieval.

Saved jobs store provenance and manual-verification state. Generated CVs and
cover letters link back through `documents.job_id`. URL-only fetching requires
`ONEPAGER_JOB_URL_ALLOWLIST` and still uses SSRF protection, size caps, and
per-source rate limits. The implementation contract is in
[`docs/job-ingestion-policy.md`](docs/job-ingestion-policy.md).

## Templates and schema controls

Built-in templates are code-owned registry entries. They are separate from
themes, and preview/build/cache keys include the selected template. All shipped
templates must pass `python3 engine/build.py` and the browser geometry gate.

`/api/schema` exposes section field metadata used by the form editor and
extraction prompt. The editor exposes YAML-backed controls for section order,
optional-section presets, contact layout, header layout, and photo layout.
Unsupported custom fields remain stored but are not promised as rendered.
