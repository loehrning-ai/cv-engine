-- OnePager — disabled-by-default external apply attempt logs.
--
-- This table is audit-only. The app never submits applications from this
-- boundary; rows record manual-open and fixture dry-run decisions.

create table if not exists public.application_apply_attempts (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null references auth.users(id) on delete cascade default auth.uid(),
  job_id uuid references public.saved_jobs(id) on delete set null,
  packet_id uuid references public.application_packets(id) on delete set null,
  action text not null,
  status text not null,
  reason_code text,
  target_url text,
  target_host text,
  target_classification text,
  submitted boolean not null default false,
  automation_performed boolean not null default false,
  readiness jsonb not null default '{}'::jsonb,
  confirmations jsonb not null default '{}'::jsonb,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.application_apply_attempts drop constraint if exists application_apply_attempts_action_check;
alter table public.application_apply_attempts add constraint application_apply_attempts_action_check
  check (action in ('manual_open', 'dry_run'));

alter table public.application_apply_attempts drop constraint if exists application_apply_attempts_status_check;
alter table public.application_apply_attempts add constraint application_apply_attempts_status_check
  check (status in ('blocked', 'manual_open_ready', 'dry_run_completed'));

alter table public.application_apply_attempts drop constraint if exists application_apply_attempts_no_submit_check;
alter table public.application_apply_attempts add constraint application_apply_attempts_no_submit_check
  check (submitted = false and automation_performed = false);

alter table public.application_apply_attempts enable row level security;

drop policy if exists "application_apply_attempts_select_own" on public.application_apply_attempts;
create policy "application_apply_attempts_select_own" on public.application_apply_attempts
  for select to authenticated
  using (
    owner_id = (select auth.uid())
    and not exists (
      select 1 from public.profiles p
      where p.id = (select auth.uid())
        and p.deletion_requested_at is not null
    )
  );

drop policy if exists "application_apply_attempts_insert_own" on public.application_apply_attempts;
create policy "application_apply_attempts_insert_own" on public.application_apply_attempts
  for insert to authenticated
  with check (
    owner_id = (select auth.uid())
    and submitted = false
    and automation_performed = false
    and (
      job_id is null
      or exists (
        select 1 from public.saved_jobs sj
        where sj.id = application_apply_attempts.job_id
          and sj.owner_id = (select auth.uid())
      )
    )
    and (
      packet_id is null
      or exists (
        select 1 from public.application_packets ap
        where ap.id = application_apply_attempts.packet_id
          and ap.owner_id = (select auth.uid())
          and ap.job_id is not distinct from application_apply_attempts.job_id
      )
    )
    and not exists (
      select 1 from public.profiles p
      where p.id = (select auth.uid())
        and p.deletion_requested_at is not null
    )
  );

drop policy if exists "application_apply_attempts_update_own" on public.application_apply_attempts;
drop policy if exists "application_apply_attempts_delete_own" on public.application_apply_attempts;

grant select, insert on public.application_apply_attempts to authenticated;
revoke update on public.application_apply_attempts from authenticated;
revoke delete on public.application_apply_attempts from authenticated;

create index if not exists application_apply_attempts_owner_created_idx
  on public.application_apply_attempts (owner_id, created_at desc);

create index if not exists application_apply_attempts_owner_job_idx
  on public.application_apply_attempts (owner_id, job_id, created_at desc)
  where job_id is not null;
