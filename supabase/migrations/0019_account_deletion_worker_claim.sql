-- CV Copilot — complete account-erasure worker contract.
--
-- Migration 0004 established the user-facing request/cancel flow, but its
-- database-only purge could not erase R2 objects or files in the persistent
-- photo volume. This migration replaces that incomplete purge path with an
-- atomic, lease-based claim consumed by the service-role purge worker.
--
-- Contract:
--   * deletion requests become irrevocable at D+30;
--   * a worker atomically claims due profiles before external cleanup starts;
--   * cancellation and claiming serialize on the same profile-row lock;
--   * failed workers retain an irrevocable claim but can be retried after the
--     lease expires;
--   * only the service_role can claim or validate work;
--   * the worker deletes external objects/files before it hard-deletes the
--     auth user, so database cascades never run after a failed external purge.

create schema if not exists private;
revoke all on schema private from public, anon, authenticated;

create table if not exists private.account_deletion_claims (
  owner_id         uuid primary key references auth.users (id) on delete cascade,
  claim_token      uuid not null,
  first_claimed_at timestamptz not null default now(),
  last_claimed_at  timestamptz not null default now(),
  lease_expires_at timestamptz not null
);

revoke all on table private.account_deletion_claims
  from public, anon, authenticated;

comment on table private.account_deletion_claims is
  'Internal irrevocable D+30 erasure claims. Access only through service-role RPCs.';

create index if not exists account_deletion_claims_lease_idx
  on private.account_deletion_claims (lease_expires_at);

-- Recreate the request function so repeated requests cannot move the deadline
-- forward, and a request cannot be restarted after the worker has claimed it.
create or replace function public.request_account_deletion()
returns timestamptz
language plpgsql
security definer
set search_path = ''
as $$
declare
  caller_id uuid := auth.uid();
  requested_at timestamptz;
begin
  if caller_id is null then
    raise exception 'not authenticated';
  end if;

  select p.deletion_requested_at
    into requested_at
    from public.profiles as p
   where p.id = caller_id
   for update;

  if not found then
    raise exception 'profile not found';
  end if;

  if exists (
    select 1
      from private.account_deletion_claims as c
     where c.owner_id = caller_id
  ) then
    raise exception 'account deletion is already being processed'
      using errcode = '55000';
  end if;

  -- Idempotent: never extend an existing 30-day deadline.
  if requested_at is not null then
    return requested_at;
  end if;

  requested_at := statement_timestamp();

  update public.profiles
     set deletion_requested_at = requested_at,
         updated_at = requested_at
   where id = caller_id;

  update public.documents
     set deleted_at = requested_at,
         updated_at = requested_at
   where owner_id = caller_id
     and deleted_at is null;

  return requested_at;
end;
$$;

-- Cancellation is permitted strictly before D+30 and only before any worker
-- claim. The row lock is the same lock used by the claim RPC, eliminating the
-- cancel/delete race at the deadline.
create or replace function public.cancel_account_deletion()
returns boolean
language plpgsql
security definer
set search_path = ''
as $$
declare
  caller_id uuid := auth.uid();
  requested_at timestamptz;
begin
  if caller_id is null then
    raise exception 'not authenticated';
  end if;

  select p.deletion_requested_at
    into requested_at
    from public.profiles as p
   where p.id = caller_id
   for update;

  if not found or requested_at is null then
    return false;
  end if;

  if requested_at <= statement_timestamp() - interval '30 days' then
    return false;
  end if;

  if exists (
    select 1
      from private.account_deletion_claims as c
     where c.owner_id = caller_id
  ) then
    return false;
  end if;

  -- Restore only documents stamped by this exact deletion request.
  update public.documents
     set deleted_at = null,
         updated_at = statement_timestamp()
   where owner_id = caller_id
     and deleted_at = requested_at;

  update public.profiles
     set deletion_requested_at = null,
         updated_at = statement_timestamp()
   where id = caller_id;

  return true;
end;
$$;

-- Atomically claim due accounts. SKIP LOCKED permits multiple worker replicas
-- without duplicate live claims. An expired lease can be reclaimed, while the
-- permanent claim row keeps cancellation closed after the first claim.
create or replace function public.claim_accounts_for_deletion(
  p_batch_size integer default 20,
  p_lease_minutes integer default 30
)
returns table (owner_id uuid, claim_token uuid)
language plpgsql
security definer
set search_path = ''
as $$
declare
  claimed_at timestamptz := statement_timestamp();
begin
  if p_batch_size < 1 or p_batch_size > 100 then
    raise exception 'batch size must be between 1 and 100';
  end if;
  if p_lease_minutes < 5 or p_lease_minutes > 1440 then
    raise exception 'lease minutes must be between 5 and 1440';
  end if;

  return query
  with candidates as (
    select p.id
      from public.profiles as p
      left join private.account_deletion_claims as c
        on c.owner_id = p.id
     where p.deletion_requested_at is not null
       and p.deletion_requested_at <= claimed_at - interval '30 days'
       and (c.owner_id is null or c.lease_expires_at <= claimed_at)
     order by p.deletion_requested_at, p.id
     for update of p skip locked
     limit p_batch_size
  ),
  claimed as (
    insert into private.account_deletion_claims as c (
      owner_id,
      claim_token,
      first_claimed_at,
      last_claimed_at,
      lease_expires_at
    )
    select candidates.id,
           gen_random_uuid(),
           claimed_at,
           claimed_at,
           claimed_at + make_interval(mins => p_lease_minutes)
      from candidates
    on conflict on constraint account_deletion_claims_pkey do update
      set claim_token = excluded.claim_token,
          last_claimed_at = excluded.last_claimed_at,
          lease_expires_at = excluded.lease_expires_at
      where c.lease_expires_at <= claimed_at
    returning c.owner_id, c.claim_token
  )
  select claimed.owner_id, claimed.claim_token
    from claimed;
end;
$$;

-- Final fail-closed check immediately before the worker calls the Auth Admin
-- hard-delete API. A reclaimed/expired token can never authorize deletion.
create or replace function public.account_deletion_claim_is_current(
  p_owner_id uuid,
  p_claim_token uuid
)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1
      from private.account_deletion_claims as c
      join public.profiles as p
        on p.id = c.owner_id
     where c.owner_id = p_owner_id
       and c.claim_token = p_claim_token
       and c.lease_expires_at > statement_timestamp()
       and p.deletion_requested_at is not null
       and p.deletion_requested_at <= statement_timestamp() - interval '30 days'
  );
$$;

-- Non-mutating readiness probe used by the worker container healthcheck. Its
-- existence proves that migration 0019 reached the database before deployment
-- is accepted.
create or replace function public.account_deletion_worker_ready()
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select to_regclass('private.account_deletion_claims') is not null;
$$;

-- Retire the database-only routine. It cannot satisfy the erasure contract
-- because Postgres cannot clean the R2 bucket or the app's photo volume.
drop function if exists public.purge_deleted_accounts(integer);

revoke execute on function public.request_account_deletion()
  from public, anon;
grant execute on function public.request_account_deletion()
  to authenticated;

revoke execute on function public.cancel_account_deletion()
  from public, anon;
grant execute on function public.cancel_account_deletion()
  to authenticated;

revoke execute on function public.claim_accounts_for_deletion(integer, integer)
  from public, anon, authenticated;
grant execute on function public.claim_accounts_for_deletion(integer, integer)
  to service_role;

revoke execute on function public.account_deletion_claim_is_current(uuid, uuid)
  from public, anon, authenticated;
grant execute on function public.account_deletion_claim_is_current(uuid, uuid)
  to service_role;

revoke execute on function public.account_deletion_worker_ready()
  from public, anon, authenticated;
grant execute on function public.account_deletion_worker_ready()
  to service_role;
