-- OnePager — Stage 11: security + performance hardening.
-- Self-hosted migration for the operator-selected Supabase project.
--
-- Three classes of fix, all IDEMPOTENT (guarded / DROP..IF EXISTS + CREATE) so
-- the migration is safe to re-run and safe whether or not a prior partial
-- apply happened:
--
--   1. Soft-delete resurrection (HIGH). A user could clear documents.deleted_at
--      via a direct PostgREST PATCH and resurrect a doc they (or the GDPR
--      erasure flow) had soft-deleted. Fixed with column-level UPDATE grants
--      mirroring 0002's managed_credits pattern: revoke the blanket UPDATE on
--      documents from `authenticated`, then re-grant UPDATE on ONLY the safe,
--      user-editable columns. deleted_at / owner_id / org_id / id / created_at
--      are deliberately EXCLUDED, so a PATCH touching them is rejected by
--      Postgres column privileges (42501) even though the row-level policy
--      would allow the row. Soft-delete still happens via the SECURITY DEFINER
--      request_account_deletion() path (and the app service role), which run as
--      owner and bypass the column grant.
--
--   2. saved_jobs.status integrity (LOW). Add a CHECK so status can only be
--      'saved' or 'applied', and column-scope its UPDATE grant the same way.
--
--   3. Performance (the 24 advisor lints). Rewrite every RLS policy to use
--      (select auth.uid()) instead of bare auth.uid() (Postgres then evaluates
--      auth.uid() ONCE as an initplan rather than per-row), add covering
--      indexes on the three unindexed FKs, and drop the unused
--      documents_owner_type_idx.

-- =====================================================================
-- 1) documents — column-scoped UPDATE grant (block deleted_at resurrection)
-- =====================================================================
-- The existing documents_update_own RLS policy still scopes WHICH ROW; this
-- further scopes WHICH COLUMNS. deleted_at is excluded → a user PATCH that
-- sets deleted_at = null is rejected with 42501.
revoke update on public.documents from authenticated;
grant update (name, slug, yaml_blob, accent, font, density, photo_key, job_id, updated_at)
  on public.documents to authenticated;

-- =====================================================================
-- 2) saved_jobs — status CHECK + column-scoped UPDATE grant
-- =====================================================================
-- CHECK constraint (guarded: add only if not already present).
do $$
begin
  if not exists (
    select 1 from pg_constraint
     where conrelid = 'public.saved_jobs'::regclass
       and conname = 'saved_jobs_status_check'
  ) then
    alter table public.saved_jobs
      add constraint saved_jobs_status_check
      check (status in ('saved', 'applied'));
  end if;
end$$;

-- Column-scoped UPDATE grant: exclude owner_id / id / created_at so a user
-- cannot re-own a row or rewrite immutable provenance via direct PATCH.
revoke update on public.saved_jobs from authenticated;
grant update (title, company, jd_text, jd_json, source_url, status, updated_at)
  on public.saved_jobs to authenticated;

-- =====================================================================
-- 3a) RLS init-plan optimization — (select auth.uid()) everywhere.
--     Postgres has no CREATE OR REPLACE POLICY, so each policy is dropped
--     IF EXISTS then re-created: idempotent and works on both an existing DB
--     (policies already exist from 0001) and a fresh rebuild.
-- =====================================================================
-- profiles
drop policy if exists "profiles_select_own" on public.profiles;
create policy "profiles_select_own" on public.profiles
  for select to authenticated
  using (id = (select auth.uid()));
drop policy if exists "profiles_insert_own" on public.profiles;
create policy "profiles_insert_own" on public.profiles
  for insert to authenticated
  with check (id = (select auth.uid()));
drop policy if exists "profiles_update_own" on public.profiles;
create policy "profiles_update_own" on public.profiles
  for update to authenticated
  using (id = (select auth.uid()))
  with check (id = (select auth.uid()));

-- saved_jobs
drop policy if exists "saved_jobs_select_own" on public.saved_jobs;
create policy "saved_jobs_select_own" on public.saved_jobs
  for select to authenticated
  using (owner_id = (select auth.uid()));
drop policy if exists "saved_jobs_insert_own" on public.saved_jobs;
create policy "saved_jobs_insert_own" on public.saved_jobs
  for insert to authenticated
  with check (owner_id = (select auth.uid()));
drop policy if exists "saved_jobs_update_own" on public.saved_jobs;
create policy "saved_jobs_update_own" on public.saved_jobs
  for update to authenticated
  using (owner_id = (select auth.uid()))
  with check (owner_id = (select auth.uid()));
drop policy if exists "saved_jobs_delete_own" on public.saved_jobs;
create policy "saved_jobs_delete_own" on public.saved_jobs
  for delete to authenticated
  using (owner_id = (select auth.uid()));

-- documents
drop policy if exists "documents_select_own" on public.documents;
create policy "documents_select_own" on public.documents
  for select to authenticated
  using (owner_id = (select auth.uid()));
drop policy if exists "documents_insert_own" on public.documents;
create policy "documents_insert_own" on public.documents
  for insert to authenticated
  with check (owner_id = (select auth.uid()));
drop policy if exists "documents_update_own" on public.documents;
create policy "documents_update_own" on public.documents
  for update to authenticated
  using (owner_id = (select auth.uid()))
  with check (owner_id = (select auth.uid()));
drop policy if exists "documents_delete_own" on public.documents;
create policy "documents_delete_own" on public.documents
  for delete to authenticated
  using (owner_id = (select auth.uid()));

-- user_llm_keys
drop policy if exists "user_llm_keys_select_own" on public.user_llm_keys;
create policy "user_llm_keys_select_own" on public.user_llm_keys
  for select to authenticated
  using (owner_id = (select auth.uid()));
drop policy if exists "user_llm_keys_insert_own" on public.user_llm_keys;
create policy "user_llm_keys_insert_own" on public.user_llm_keys
  for insert to authenticated
  with check (owner_id = (select auth.uid()));
drop policy if exists "user_llm_keys_update_own" on public.user_llm_keys;
create policy "user_llm_keys_update_own" on public.user_llm_keys
  for update to authenticated
  using (owner_id = (select auth.uid()))
  with check (owner_id = (select auth.uid()));
drop policy if exists "user_llm_keys_delete_own" on public.user_llm_keys;
create policy "user_llm_keys_delete_own" on public.user_llm_keys
  for delete to authenticated
  using (owner_id = (select auth.uid()));

-- pdf_artifacts
drop policy if exists "pdf_artifacts_select_own" on public.pdf_artifacts;
create policy "pdf_artifacts_select_own" on public.pdf_artifacts
  for select to authenticated
  using (owner_id = (select auth.uid()));
drop policy if exists "pdf_artifacts_insert_own" on public.pdf_artifacts;
create policy "pdf_artifacts_insert_own" on public.pdf_artifacts
  for insert to authenticated
  with check (owner_id = (select auth.uid()));
drop policy if exists "pdf_artifacts_update_own" on public.pdf_artifacts;
create policy "pdf_artifacts_update_own" on public.pdf_artifacts
  for update to authenticated
  using (owner_id = (select auth.uid()))
  with check (owner_id = (select auth.uid()));
drop policy if exists "pdf_artifacts_delete_own" on public.pdf_artifacts;
create policy "pdf_artifacts_delete_own" on public.pdf_artifacts
  for delete to authenticated
  using (owner_id = (select auth.uid()));

-- usage_events (append-only: select + insert only)
drop policy if exists "usage_events_select_own" on public.usage_events;
create policy "usage_events_select_own" on public.usage_events
  for select to authenticated
  using (owner_id = (select auth.uid()));
drop policy if exists "usage_events_insert_own" on public.usage_events;
create policy "usage_events_insert_own" on public.usage_events
  for insert to authenticated
  with check (owner_id = (select auth.uid()));

-- =====================================================================
-- 3b) Covering indexes on the unindexed foreign keys.
-- =====================================================================
create index if not exists documents_job_id_idx
  on public.documents (job_id);
create index if not exists pdf_artifacts_owner_id_idx
  on public.pdf_artifacts (owner_id);
create index if not exists saved_jobs_owner_id_idx
  on public.saved_jobs (owner_id);

-- =====================================================================
-- 3c) Drop the unused composite index flagged by the advisor.
--     documents_owner_updated_idx already covers owner-scoped lookups; the
--     (owner_id, type) index is reported unused. IF EXISTS keeps it idempotent.
-- =====================================================================
drop index if exists public.documents_owner_type_idx;
