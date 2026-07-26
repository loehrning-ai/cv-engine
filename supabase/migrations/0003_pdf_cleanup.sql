-- OnePager — Stage 8: stale-PDF artifact cleanup.
-- Self-hosted migration for the operator-selected Supabase project.
--
-- pdf_artifacts rows accumulate as users re-render CVs/cover letters: every
-- content change supersedes the prior PDF (marked stale=true) and inserts a
-- fresh row. Without a sweep the table grows unbounded. This adds the single
-- sanctioned cleanup path: a SECURITY DEFINER function that deletes artifacts
-- that are EITHER already stale OR simply older than a retention window.
--
-- SCOPE: this deletes Postgres ROWS only. It does NOT delete the underlying R2
-- objects — R2 lifecycle rules (an operator/deploy step) handle physical object
-- expiry. That decoupling is deliberate and deferred: the DB stays the source
-- of truth for "what is live", and orphaned R2 bytes age out on their own.
--
-- SCHEDULING is an operator/deploy step (NOT wired here): run this on a cadence
-- via pg_cron (e.g. `select cron.schedule('purge-pdfs','0 3 * * *',
-- 'select public.purge_stale_pdf_artifacts(30)')`) or a scheduled Supabase Edge
-- Function. The function is idempotent and safe to call repeatedly.

-- SECURITY DEFINER + pinned empty search_path (satisfies the advisor and
-- prevents search_path hijacking). Runs as the function owner so a scheduled
-- job (or an operator) can sweep ALL tenants' stale rows in one call — this is
-- a maintenance routine, not a per-user RLS path, which is exactly why EXECUTE
-- is revoked from anon/authenticated below (only the table owner / a SECURITY
-- DEFINER scheduler context invokes it).
--
-- Deletes a row when EITHER:
--   * it is already marked stale (superseded by a newer render), OR
--   * it is older than `older_than_days` (default 30) regardless of stale flag
--     — an old artifact whose document is untouched still ages out.
-- Returns the number of rows deleted (so a scheduler can log the sweep).
create function public.purge_stale_pdf_artifacts(older_than_days int default 30)
returns integer
language plpgsql
security definer
set search_path = ''
as $$
declare
  deleted_count integer;
begin
  with removed as (
    delete from public.pdf_artifacts
     where stale = true
        or generated_at < now() - make_interval(days => greatest(older_than_days, 0))
    returning 1
  )
  select count(*) into deleted_count from removed;
  return deleted_count;
end;
$$;

-- Lock down EXECUTE: this is a maintenance routine, never a user-facing RPC.
-- Neither anon nor authenticated may invoke it (a user must not be able to
-- mass-delete artifacts, even their own, through an unaudited path — normal
-- per-row deletes happen under RLS via the documents ON DELETE CASCADE / the
-- app's own stale-marking). Only the owner / a scheduler context runs it.
revoke execute on function public.purge_stale_pdf_artifacts(int) from public, anon, authenticated;
