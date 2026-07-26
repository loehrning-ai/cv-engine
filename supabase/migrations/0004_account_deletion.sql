-- OnePager — Stage 10: right-to-erasure (GDPR Art. 17).
-- Self-hosted migration for the operator-selected Supabase project.
--
-- Erasure is a two-phase model so an accidental or coerced delete is
-- recoverable for a grace window, while the user's data effectively
-- disappears immediately:
--
--   1. request_account_deletion()  (user-facing, immediate)
--        • stamps profiles.deletion_requested_at = now() for auth.uid()
--        • SOFT-deletes the user's documents (deleted_at = now()) so they
--          vanish from every list/get endpoint at once (those all filter
--          `deleted_at is null`). Nothing is physically gone yet.
--   2. purge_deleted_accounts(grace_days)  (operator/cron, D+grace)
--        • HARD-deletes auth.users for any profile whose request is older
--          than the grace window. Every owned row (documents, saved_jobs,
--          user_llm_keys, pdf_artifacts, usage_events, profiles) is removed
--          by the existing `on delete cascade` FKs to auth.users(id).
--
-- A cancel path (cancel_account_deletion) lets a user who is still inside
-- the grace window undo the request; it clears the flag and un-soft-deletes
-- the documents that were soft-deleted by the request (best-effort: it
-- restores documents whose deleted_at >= the recorded request time).
--
-- All three functions are SECURITY DEFINER with `set search_path = ''`
-- (satisfies the advisor + prevents search_path hijacking). The two
-- user-facing ones act ONLY on auth.uid(); the purge is an operator routine
-- with EXECUTE revoked from anon/authenticated.

-- 1) The soft-delete marker. Nullable: most rows never have it set.
alter table public.profiles
  add column if not exists deletion_requested_at timestamptz;

-- 2) User-facing: request erasure of my own account (immediate soft-delete).
--    SECURITY DEFINER so it can stamp the profile + soft-delete documents in
--    one atomic transaction acting solely on auth.uid(). It must run as owner
--    because deletion_requested_at is not in the column-UPDATE grant given to
--    `authenticated` (migration 0002 restricts that grant), and to keep the
--    profile + documents writes in a single audited path.
create function public.request_account_deletion()
returns timestamptz
language plpgsql
security definer
set search_path = ''
as $$
declare
  requested_at timestamptz := now();
begin
  if auth.uid() is null then
    raise exception 'not authenticated';
  end if;

  -- Stamp the request on the caller's own profile.
  update public.profiles
     set deletion_requested_at = requested_at,
         updated_at = requested_at
   where id = auth.uid();

  -- Soft-delete the caller's documents so they disappear from every
  -- list/get endpoint immediately (all of which filter deleted_at is null).
  -- Only touch rows not already soft-deleted, so a later cancel can restore
  -- exactly the set this request removed.
  update public.documents
     set deleted_at = requested_at,
         updated_at = requested_at
   where owner_id = auth.uid()
     and deleted_at is null;

  return requested_at;
end;
$$;

-- 3) User-facing: cancel a pending deletion (still inside the grace window).
--    Clears the flag and restores documents soft-deleted at/after the request.
create function public.cancel_account_deletion()
returns boolean
language plpgsql
security definer
set search_path = ''
as $$
declare
  requested_at timestamptz;
begin
  if auth.uid() is null then
    raise exception 'not authenticated';
  end if;

  select deletion_requested_at into requested_at
    from public.profiles
   where id = auth.uid();

  if requested_at is null then
    return false;  -- nothing pending
  end if;

  -- Restore the documents this request soft-deleted (deleted_at >= request).
  update public.documents
     set deleted_at = null,
         updated_at = now()
   where owner_id = auth.uid()
     and deleted_at is not null
     and deleted_at >= requested_at;

  update public.profiles
     set deletion_requested_at = null,
         updated_at = now()
   where id = auth.uid();

  return true;
end;
$$;

-- 4) Operator/cron: hard-purge accounts past their grace window.
--    Deletes auth.users rows; every owned table cascades via its FK to
--    auth.users(id) on delete cascade (profiles, documents, saved_jobs,
--    user_llm_keys, pdf_artifacts, usage_events). This is a maintenance
--    routine, NOT a user-facing RPC — EXECUTE is revoked from anon +
--    authenticated below so only the table owner / a scheduler context runs
--    it. Returns the number of accounts purged so a scheduler can log it.
--
--    SCHEDULING is an operator/deploy step (NOT wired here): run on a cadence
--    via pg_cron (e.g. `select cron.schedule('purge-accounts','30 3 * * *',
--    'select public.purge_deleted_accounts(30)')`) or a scheduled Supabase
--    Edge Function. The function is idempotent and safe to call repeatedly.
create function public.purge_deleted_accounts(grace_days int default 30)
returns integer
language plpgsql
security definer
set search_path = ''
as $$
declare
  purged_count integer;
begin
  with doomed as (
    select id
      from public.profiles
     where deletion_requested_at is not null
       and deletion_requested_at < now() - make_interval(days => greatest(grace_days, 0))
  ),
  removed as (
    delete from auth.users
     where id in (select id from doomed)
    returning 1
  )
  select count(*) into purged_count from removed;
  return purged_count;
end;
$$;

-- Lock down EXECUTE.
--   * request/cancel: the user's own erasure controls → authenticated only.
--   * purge: an operator/scheduler maintenance routine that hard-deletes
--     across tenants → never callable by anon or authenticated.
revoke execute on function public.request_account_deletion() from public, anon;
grant execute on function public.request_account_deletion() to authenticated;

revoke execute on function public.cancel_account_deletion() from public, anon;
grant execute on function public.cancel_account_deletion() to authenticated;

revoke execute on function public.purge_deleted_accounts(int) from public, anon, authenticated;
