-- OnePager -- prevent mutable catalog state from claiming an application.
--
-- Historical `saved_jobs.status = 'applied'` rows remain readable and may be
-- changed to an honest lifecycle state. New inserts and transitions into that
-- legacy value are rejected even when an authenticated client writes through
-- PostgREST directly instead of the application server. External-action state
-- belongs exclusively to append-only `application_apply_attempts` evidence.

create or replace function public.reject_evidence_free_applied_job_status()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if tg_op = 'INSERT' then
    if new.status = 'applied' then
      raise exception 'applied status requires immutable application evidence'
        using errcode = '23514';
    end if;
  elsif new.status = 'applied' and old.status is distinct from 'applied' then
    raise exception 'applied status requires immutable application evidence'
      using errcode = '23514';
  end if;
  return new;
end;
$$;

drop trigger if exists reject_evidence_free_applied_job_status_on_write
  on public.saved_jobs;
create trigger reject_evidence_free_applied_job_status_on_write
before insert or update of status on public.saved_jobs
for each row execute function public.reject_evidence_free_applied_job_status();

revoke all on function public.reject_evidence_free_applied_job_status()
  from public, anon, authenticated;
