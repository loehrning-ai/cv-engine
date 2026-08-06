-- OnePager — bound the derived-PDF cleanup outbox retry rate.
--
-- `record_pdf_artifact_cleanup_failure` incremented `attempt_count` and stamped
-- `last_attempt_at`, but never advanced `not_before`. The worker's batch query
-- selects on `not_before <= now()`, so a row whose R2 DELETE fails permanently
-- (revoked key, deleted bucket, object owned by another account) was re-claimed
-- on every single cycle: a hot loop of failing network calls, matching log
-- spam, and no signal that the row needs attention.
--
-- Failures now back off exponentially — 1, 2, 4, 8, 16, 32 minutes — capped at
-- one hour so a transient outage still drains promptly once it clears.
--
-- The row is deliberately NEVER dropped. These entries exist because a user
-- deleted a document or erased an account, so the object must eventually leave
-- R2; discarding the queue row would orphan it permanently and silently. A
-- chronically failing row therefore parks at the hourly cap and stays visible
-- through `attempt_count`, which the worker logs.
--
-- Signature and return type are unchanged, so this is a plain replace: existing
-- grants and the release schema contract continue to hold.

create or replace function public.record_pdf_artifact_cleanup_failure(
  p_owner_id uuid,
  p_cleanup_id uuid,
  p_claim_token uuid
)
returns boolean
language plpgsql
security definer
set search_path = ''
set lock_timeout = '2s'
as $$
declare
  updated_id uuid;
begin
  if p_owner_id is null or p_cleanup_id is null or p_claim_token is null then
    raise exception 'owner, cleanup id, and claim token are required'
      using errcode = '22023';
  end if;

  update public.pdf_artifact_cleanup_queue as q
     set attempt_count = q.attempt_count + 1,
         last_attempt_at = now(),
         -- least() caps the doubling; the inner least() keeps the exponent
         -- small enough that power() cannot overflow the interval on a row
         -- that has been failing for a very long time.
         not_before = now() + least(
           interval '1 minute' * power(2, least(q.attempt_count, 6)),
           interval '1 hour'
         ),
         claim_token = null,
         claimed_at = null
   where q.id = p_cleanup_id
     and q.owner_id = p_owner_id
     and q.claim_token = p_claim_token
  returning q.id into updated_id;

  return updated_id is not null;
end;
$$;

revoke all on function public.record_pdf_artifact_cleanup_failure(uuid, uuid, uuid)
  from public, anon, authenticated;
grant execute on function public.record_pdf_artifact_cleanup_failure(uuid, uuid, uuid)
  to service_role;
