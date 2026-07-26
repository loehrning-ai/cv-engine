-- OnePager — plan 002 stage 6: per-user jobs CATALOG metadata + close the
-- cross-tenant documents.job_id hole.
-- Self-hosted migration for the operator-selected Supabase project and region.
--
-- Fully IDEMPOTENT (ADD COLUMN IF NOT EXISTS / DROP..IF EXISTS + ADD) so it is
-- safe to re-run and safe on a fresh rebuild. Two independent concerns:
--
--   1. saved_jobs gains the catalog curation columns (field/deadline/
--      confidence/why_fit), the status enum widens to the full lifecycle, and
--      the column-scoped UPDATE grant from 0005 is RE-ISSUED to include the new
--      columns (else they are silently un-writable for `authenticated` — the
--      0005 grant excludes them, so a PATCH returns 403/no-op).
--
--   2. (SECURITY MUST-FIX) The documents INSERT + UPDATE policies are recreated
--      with a WITH CHECK that the stamped job_id is null OR owned by the caller.
--      0005's policies only verify owner_id = auth.uid() — they do NOT verify
--      the referenced saved_jobs row is owned, so user B could stamp A's job_id
--      and, on A deleting that job, ON DELETE SET NULL would mutate B's row
--      cross-tenant. The catalog makes this routine. Defense-in-depth pairs this
--      DB backstop with the app-layer ownership check in the routes/service.

-- =====================================================================
-- 1) saved_jobs — catalog metadata columns (all nullable → old rows valid).
-- =====================================================================
alter table public.saved_jobs
  add column if not exists field      text,
  add column if not exists deadline   date,       -- nullable = "Rolling"
  add column if not exists confidence smallint,    -- 0-100 fit %, nullable
  add column if not exists why_fit    text;        -- user-authored "Your note"

-- =====================================================================
-- 2) saved_jobs.status — EXPAND the enum. The 0005 constraint was added GUARDED
--    (status in ('saved','applied')); a second guarded add would be a no-op and
--    silently reject the new statuses, so DROP then ADD explicitly.
-- =====================================================================
alter table public.saved_jobs drop constraint if exists saved_jobs_status_check;
alter table public.saved_jobs add  constraint saved_jobs_status_check
  check (status in ('saved', 'open', 'speculative', 'applied', 'closed'));

-- =====================================================================
-- 3) Range / length CHECKs for the new columns (DB backstop; app validates too).
--    confidence: 0-100 inclusive (nullable allowed). why_fit: capped length so a
--    runaway paste can't bloat the row.
-- =====================================================================
alter table public.saved_jobs drop constraint if exists saved_jobs_confidence_check;
alter table public.saved_jobs add  constraint saved_jobs_confidence_check
  check (confidence is null or confidence between 0 and 100);

alter table public.saved_jobs drop constraint if exists saved_jobs_why_fit_len_check;
alter table public.saved_jobs add  constraint saved_jobs_why_fit_len_check
  check (why_fit is null or char_length(why_fit) <= 2000);

-- =====================================================================
-- 4) RE-ISSUE the column-scoped UPDATE grant (0005 revoked the blanket grant and
--    re-granted only the original 7 columns). Include the new catalog columns;
--    keep owner_id / id / created_at EXCLUDED so a PATCH cannot re-own a row or
--    rewrite immutable provenance.
-- =====================================================================
revoke update on public.saved_jobs from authenticated;
grant update (title, company, jd_text, jd_json, source_url, status,
              field, deadline, confidence, why_fit, updated_at)
  on public.saved_jobs to authenticated;

-- =====================================================================
-- 5) Index for the has_cover grouped derivation (count of a job's cover-letter
--    documents). Partial index keyed on the exact predicate the list query uses.
-- =====================================================================
create index if not exists documents_job_cover_idx
  on public.documents (job_id)
  where type = 'cover_letter' and deleted_at is null;

-- =====================================================================
-- 6) (SECURITY MUST-FIX) Close the cross-tenant job_id hole at the DB layer.
--    Recreate the documents INSERT + UPDATE policies so the WITH CHECK also
--    requires job_id to be null OR reference a saved_jobs row the caller owns.
--    The existing owner_id check is PRESERVED. Postgres has no CREATE OR REPLACE
--    POLICY, so drop IF EXISTS then create (idempotent on live + fresh rebuild).
-- =====================================================================
drop policy if exists "documents_insert_own" on public.documents;
create policy "documents_insert_own" on public.documents
  for insert to authenticated
  with check (
    owner_id = (select auth.uid())
    and (
      job_id is null
      or exists (
        select 1 from public.saved_jobs sj
        where sj.id = documents.job_id
          and sj.owner_id = (select auth.uid())
      )
    )
  );

drop policy if exists "documents_update_own" on public.documents;
create policy "documents_update_own" on public.documents
  for update to authenticated
  using (owner_id = (select auth.uid()))
  with check (
    owner_id = (select auth.uid())
    and (
      job_id is null
      or exists (
        select 1 from public.saved_jobs sj
        where sj.id = documents.job_id
          and sj.owner_id = (select auth.uid())
      )
    )
  );
