-- OnePager — Stage 4: tamper-proof managed credits.
-- Self-hosted migration for the operator-selected Supabase project.
--
-- Credit self-tampering is the #1 risk here: a user could otherwise
-- `update profiles set managed_credits_remaining = 999` via the RLS client
-- and mint themselves unlimited free generations. We close that hole in two
-- moves:
--   1. Revoke the blanket UPDATE on profiles from `authenticated`, then
--      grant UPDATE on ONLY the safe, user-editable columns. The
--      managed_credits_remaining column is deliberately excluded, so a
--      direct UPDATE touching it is rejected by Postgres column privileges
--      (even though the row-level USING/WITH CHECK policy would allow the row).
--   2. Decrement credits ONLY through a SECURITY DEFINER function that runs
--      as the table owner (bypassing the column grant) and is the single
--      audited path that can lower the balance. The function also writes the
--      usage_events audit row in the same transaction.

-- 1) Column-scoped UPDATE grant. `authenticated` may edit display_name,
--    default_language, email, updated_at — never managed_credits_remaining.
--    (The existing profiles_update_own RLS policy still scopes which ROW;
--    this further scopes which COLUMNS.)
revoke update on public.profiles from authenticated;
grant update (display_name, default_language, email, updated_at)
  on public.profiles to authenticated;

-- 2) The only sanctioned way to spend a managed credit.
--    SECURITY DEFINER + pinned empty search_path (satisfies the advisor and
--    prevents search_path hijacking). Runs as the function owner, so it can
--    update managed_credits_remaining despite the column grant above.
--    Atomic: the conditional UPDATE and the audit insert share one tx.
--
--    NOTE: the security advisor emits a WARN that an authenticated-callable
--    SECURITY DEFINER function exists. That is INTENTIONAL here — this is the
--    sanctioned RPC. It is safe because it (a) pins search_path, (b) only ever
--    DECREMENTS, guarded by `> 0`, (c) acts solely on auth.uid(), and (d) has
--    EXECUTE revoked from public/anon. Switching it to SECURITY INVOKER would
--    defeat the tamper-proofing (it must run as owner to touch the revoked
--    column).
create function public.consume_managed_credit(p_kind text)
returns boolean
language plpgsql
security definer
set search_path = ''
as $$
begin
  update public.profiles
     set managed_credits_remaining = managed_credits_remaining - 1
   where id = auth.uid()
     and managed_credits_remaining > 0;
  if not found then
    return false;
  end if;
  insert into public.usage_events(owner_id, kind, used_managed_credit)
  values (auth.uid(), p_kind, true);
  return true;
end;
$$;

-- Lock down EXECUTE: only authenticated callers may spend a credit; never
-- anon, never the public pseudo-role.
revoke execute on function public.consume_managed_credit(text) from public, anon;
grant execute on function public.consume_managed_credit(text) to authenticated;
