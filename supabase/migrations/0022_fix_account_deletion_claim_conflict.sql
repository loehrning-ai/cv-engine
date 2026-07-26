-- CV Copilot — repair the live worker claim RPC.
--
-- A RETURNS TABLE output column is a PL/pgSQL variable. Using
-- A column-list conflict target therefore makes owner_id ambiguous at runtime.
-- Name the primary-key constraint explicitly so live claims execute.

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

revoke execute on function public.claim_accounts_for_deletion(integer, integer)
  from public, anon, authenticated;
grant execute on function public.claim_accounts_for_deletion(integer, integer)
  to service_role;
