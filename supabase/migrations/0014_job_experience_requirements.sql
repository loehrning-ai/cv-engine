-- OnePager — deterministic years evidence for jobs/import previews.

alter table public.saved_jobs
  add column if not exists experience_requirements jsonb not null default '{}'::jsonb;

alter table public.job_import_results
  add column if not exists experience_requirements jsonb not null default '{}'::jsonb;

revoke update on public.saved_jobs from authenticated;
grant update (title, company, jd_text, jd_json, source_url, source_metadata,
              content_hash, last_seen_at, expires_at,
              status, field, deadline, confidence, why_fit,
              experience_requirements, updated_at)
  on public.saved_jobs to authenticated;

revoke update on public.job_import_results from authenticated;
