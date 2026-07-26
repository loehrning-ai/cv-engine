-- OnePager — plans 059/060: job import provenance and run tracking.
--
-- Additive and idempotent. Imported/manual jobs keep using `saved_jobs` so
-- existing job detail, tailoring, cover letters, and `documents.job_id` packet
-- linkage remain intact. Provenance is immutable through general PATCH.

create table if not exists public.job_import_runs (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null references auth.users(id) on delete cascade default auth.uid(),
  provider text not null,
  query text,
  location text,
  status text not null default 'running',
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  requested_limit integer,
  imported_count integer not null default 0,
  skipped_count integer not null default 0,
  error text,
  metadata jsonb not null default '{}'::jsonb
);

alter table public.job_import_runs drop constraint if exists job_import_runs_status_check;
alter table public.job_import_runs add constraint job_import_runs_status_check
  check (status in ('running', 'completed', 'failed', 'partial'));

alter table public.job_import_runs enable row level security;

drop policy if exists "job_import_runs_select_own" on public.job_import_runs;
create policy "job_import_runs_select_own" on public.job_import_runs
  for select to authenticated
  using (owner_id = (select auth.uid()));

drop policy if exists "job_import_runs_insert_own" on public.job_import_runs;
create policy "job_import_runs_insert_own" on public.job_import_runs
  for insert to authenticated
  with check (
    owner_id = (select auth.uid())
    and not exists (
      select 1 from public.profiles p
      where p.id = (select auth.uid())
        and p.deletion_requested_at is not null
    )
  );

drop policy if exists "job_import_runs_update_own" on public.job_import_runs;
create policy "job_import_runs_update_own" on public.job_import_runs
  for update to authenticated
  using (owner_id = (select auth.uid()))
  with check (
    owner_id = (select auth.uid())
    and not exists (
      select 1 from public.profiles p
      where p.id = (select auth.uid())
        and p.deletion_requested_at is not null
    )
  );

drop policy if exists "job_import_runs_delete_own" on public.job_import_runs;
create policy "job_import_runs_delete_own" on public.job_import_runs
  for delete to authenticated
  using (owner_id = (select auth.uid()));

grant select, insert, delete on public.job_import_runs to authenticated;
revoke update on public.job_import_runs from authenticated;
grant update (status, finished_at, imported_count, skipped_count, error, metadata)
  on public.job_import_runs to authenticated;

create index if not exists job_import_runs_owner_started_idx
  on public.job_import_runs (owner_id, started_at desc);

create table if not exists public.job_import_results (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null references auth.users(id) on delete cascade default auth.uid(),
  import_run_id uuid not null references public.job_import_runs(id) on delete cascade,
  source_provider text not null,
  source_type text not null,
  source_external_id text,
  title text,
  company text,
  jd_text text not null,
  jd_json jsonb not null default '{}'::jsonb,
  source_url text,
  source_metadata jsonb not null default '{}'::jsonb,
  source_country text,
  source_region text,
  source_location text,
  remote_mode text,
  salary_text text,
  seniority text,
  verification_status text not null default 'user_provided',
  content_hash text,
  last_seen_at timestamptz,
  expires_at timestamptz,
  selected boolean not null default false,
  saved_job_id uuid references public.saved_jobs(id) on delete set null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.job_import_results drop constraint if exists job_import_results_source_provider_check;
alter table public.job_import_results add constraint job_import_results_source_provider_check
  check (source_provider in ('manual', 'linkedin', 'approved_adapter', 'other'));

alter table public.job_import_results drop constraint if exists job_import_results_source_type_check;
alter table public.job_import_results add constraint job_import_results_source_type_check
  check (source_type in ('manual_paste', 'manual_url_reference', 'csv_import', 'approved_adapter'));

alter table public.job_import_results drop constraint if exists job_import_results_verification_status_check;
alter table public.job_import_results add constraint job_import_results_verification_status_check
  check (verification_status in ('user_provided', 'needs_review', 'verified', 'stale'));

alter table public.job_import_results enable row level security;

drop policy if exists "job_import_results_select_own" on public.job_import_results;
create policy "job_import_results_select_own" on public.job_import_results
  for select to authenticated
  using (owner_id = (select auth.uid()));

drop policy if exists "job_import_results_insert_own" on public.job_import_results;
create policy "job_import_results_insert_own" on public.job_import_results
  for insert to authenticated
  with check (
    owner_id = (select auth.uid())
    and exists (
      select 1 from public.job_import_runs jir
      where jir.id = job_import_results.import_run_id
        and jir.owner_id = (select auth.uid())
    )
    and not exists (
      select 1 from public.profiles p
      where p.id = (select auth.uid())
        and p.deletion_requested_at is not null
    )
  );

drop policy if exists "job_import_results_update_own" on public.job_import_results;
create policy "job_import_results_update_own" on public.job_import_results
  for update to authenticated
  using (owner_id = (select auth.uid()))
  with check (
    owner_id = (select auth.uid())
    and exists (
      select 1 from public.job_import_runs jir
      where jir.id = job_import_results.import_run_id
        and jir.owner_id = (select auth.uid())
    )
    and (
      saved_job_id is null
      or exists (
        select 1 from public.saved_jobs sj
        where sj.id = job_import_results.saved_job_id
          and sj.owner_id = (select auth.uid())
      )
    )
    and not exists (
      select 1 from public.profiles p
      where p.id = (select auth.uid())
        and p.deletion_requested_at is not null
    )
  );

drop policy if exists "job_import_results_delete_own" on public.job_import_results;
create policy "job_import_results_delete_own" on public.job_import_results
  for delete to authenticated
  using (owner_id = (select auth.uid()));

grant select, insert, delete on public.job_import_results to authenticated;
revoke update on public.job_import_results from authenticated;
grant update (verification_status, selected, saved_job_id, updated_at)
  on public.job_import_results to authenticated;

create index if not exists job_import_results_owner_run_idx
  on public.job_import_results (owner_id, import_run_id, created_at desc);

create unique index if not exists job_import_results_owner_source_external_uidx
  on public.job_import_results (owner_id, source_provider, source_external_id)
  where source_external_id is not null;

alter table public.saved_jobs
  add column if not exists import_run_id uuid references public.job_import_runs(id) on delete set null,
  add column if not exists source_provider text,
  add column if not exists source_type text,
  add column if not exists source_external_id text,
  add column if not exists source_fetched_at timestamptz,
  add column if not exists source_metadata jsonb not null default '{}'::jsonb,
  add column if not exists source_country text,
  add column if not exists source_region text,
  add column if not exists source_location text,
  add column if not exists remote_mode text,
  add column if not exists salary_text text,
  add column if not exists seniority text,
  add column if not exists verification_status text not null default 'user_provided',
  add column if not exists content_hash text,
  add column if not exists last_seen_at timestamptz,
  add column if not exists expires_at timestamptz;

alter table public.saved_jobs drop constraint if exists saved_jobs_source_provider_check;
alter table public.saved_jobs add constraint saved_jobs_source_provider_check
  check (source_provider is null or source_provider in ('manual', 'linkedin', 'approved_adapter', 'other'));

alter table public.saved_jobs drop constraint if exists saved_jobs_source_type_check;
alter table public.saved_jobs add constraint saved_jobs_source_type_check
  check (source_type is null or source_type in ('manual_paste', 'manual_url_reference', 'csv_import', 'approved_adapter'));

alter table public.saved_jobs drop constraint if exists saved_jobs_verification_status_check;
alter table public.saved_jobs add constraint saved_jobs_verification_status_check
  check (verification_status in ('user_provided', 'needs_review', 'verified', 'stale'));

create unique index if not exists saved_jobs_owner_source_external_uidx
  on public.saved_jobs (owner_id, source_provider, source_external_id)
  where source_provider is not null and source_external_id is not null;

create index if not exists saved_jobs_owner_import_run_idx
  on public.saved_jobs (owner_id, import_run_id);

revoke update on public.saved_jobs from authenticated;
grant update (title, company, jd_text, jd_json, source_url, status,
              field, deadline, confidence, why_fit, updated_at)
  on public.saved_jobs to authenticated;

drop policy if exists "saved_jobs_insert_own" on public.saved_jobs;
create policy "saved_jobs_insert_own" on public.saved_jobs
  for insert to authenticated
  with check (
    owner_id = (select auth.uid())
    and (
      import_run_id is null
      or exists (
        select 1 from public.job_import_runs jir
        where jir.id = saved_jobs.import_run_id
          and jir.owner_id = (select auth.uid())
      )
    )
    and not exists (
      select 1 from public.profiles p
      where p.id = (select auth.uid())
        and p.deletion_requested_at is not null
    )
  );

drop policy if exists "saved_jobs_update_own" on public.saved_jobs;
create policy "saved_jobs_update_own" on public.saved_jobs
  for update to authenticated
  using (owner_id = (select auth.uid()))
  with check (
    owner_id = (select auth.uid())
    and (
      import_run_id is null
      or exists (
        select 1 from public.job_import_runs jir
        where jir.id = saved_jobs.import_run_id
          and jir.owner_id = (select auth.uid())
      )
    )
    and not exists (
      select 1 from public.profiles p
      where p.id = (select auth.uid())
        and p.deletion_requested_at is not null
    )
  );
