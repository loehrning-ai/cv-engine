-- OnePager — application packet lineage.
--
-- CVs and cover letters remain `documents` rows. This table stores only the
-- packet join rows: related ids, hashes, status, model/version metadata, and an
-- ATS score snapshot.

create table if not exists public.application_packets (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null references auth.users(id) on delete cascade default auth.uid(),
  job_id uuid references public.saved_jobs(id) on delete set null,
  source_cv_id uuid references public.documents(id) on delete set null,
  tailored_cv_id uuid references public.documents(id) on delete set null,
  cover_letter_id uuid references public.documents(id) on delete set null,
  status text not null default 'packet_created',
  review_status text not null default 'reviewed',
  provider text,
  model text,
  prompt_version text,
  score_system_id text not null default 'semantic_fit',
  score_snapshot jsonb not null default '{}'::jsonb,
  source_cv_hash text,
  job_content_hash text,
  tailored_cv_hash text,
  cover_letter_hash text,
  input_hash text,
  output_hash text,
  packet_version integer not null default 1,
  profile_answers_complete boolean not null default false,
  legal_answers_confirmed boolean not null default false,
  visa_answers_confirmed boolean not null default false,
  archived_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.application_packets drop constraint if exists application_packets_status_check;
alter table public.application_packets add constraint application_packets_status_check
  check (status in ('packet_created', 'review_ready', 'review_applied', 'archived'));

alter table public.application_packets drop constraint if exists application_packets_review_status_check;
alter table public.application_packets add constraint application_packets_review_status_check
  check (review_status in ('not_reviewed', 'review_required', 'reviewed', 'review_skipped'));

alter table public.application_packets drop constraint if exists application_packets_packet_version_check;
alter table public.application_packets add constraint application_packets_packet_version_check
  check (packet_version > 0);

alter table public.application_packets drop constraint if exists application_packets_score_system_check;
alter table public.application_packets add constraint application_packets_score_system_check
  check (btrim(score_system_id) <> '');

alter table public.application_packets enable row level security;

drop policy if exists "application_packets_select_own" on public.application_packets;
create policy "application_packets_select_own" on public.application_packets
  for select to authenticated
  using (
    owner_id = (select auth.uid())
    and not exists (
      select 1 from public.profiles p
      where p.id = (select auth.uid())
        and p.deletion_requested_at is not null
    )
  );

drop policy if exists "application_packets_insert_own" on public.application_packets;
create policy "application_packets_insert_own" on public.application_packets
  for insert to authenticated
  with check (
    owner_id = (select auth.uid())
    and (
      job_id is null
      or exists (
        select 1 from public.saved_jobs sj
        where sj.id = application_packets.job_id
          and sj.owner_id = (select auth.uid())
      )
    )
    and (
      source_cv_id is null
      or exists (
        select 1 from public.documents d
        where d.id = application_packets.source_cv_id
          and d.owner_id = (select auth.uid())
          and d.type = 'cv'
      )
    )
    and (
      tailored_cv_id is null
      or exists (
        select 1 from public.documents d
        where d.id = application_packets.tailored_cv_id
          and d.owner_id = (select auth.uid())
          and d.type = 'cv'
          and d.job_id = application_packets.job_id
      )
    )
    and (
      cover_letter_id is null
      or exists (
        select 1 from public.documents d
        where d.id = application_packets.cover_letter_id
          and d.owner_id = (select auth.uid())
          and d.type = 'cover_letter'
          and d.job_id = application_packets.job_id
      )
    )
    and not exists (
      select 1 from public.profiles p
      where p.id = (select auth.uid())
        and p.deletion_requested_at is not null
    )
  );

drop policy if exists "application_packets_update_own" on public.application_packets;
create policy "application_packets_update_own" on public.application_packets
  for update to authenticated
  using (
    owner_id = (select auth.uid())
    and not exists (
      select 1 from public.profiles p
      where p.id = (select auth.uid())
        and p.deletion_requested_at is not null
    )
  )
  with check (
    owner_id = (select auth.uid())
    and (
      job_id is null
      or exists (
        select 1 from public.saved_jobs sj
        where sj.id = application_packets.job_id
          and sj.owner_id = (select auth.uid())
      )
    )
    and (
      source_cv_id is null
      or exists (
        select 1 from public.documents d
        where d.id = application_packets.source_cv_id
          and d.owner_id = (select auth.uid())
          and d.type = 'cv'
      )
    )
    and (
      tailored_cv_id is null
      or exists (
        select 1 from public.documents d
        where d.id = application_packets.tailored_cv_id
          and d.owner_id = (select auth.uid())
          and d.type = 'cv'
          and d.job_id = application_packets.job_id
      )
    )
    and (
      cover_letter_id is null
      or exists (
        select 1 from public.documents d
        where d.id = application_packets.cover_letter_id
          and d.owner_id = (select auth.uid())
          and d.type = 'cover_letter'
          and d.job_id = application_packets.job_id
      )
    )
    and not exists (
      select 1 from public.profiles p
      where p.id = (select auth.uid())
        and p.deletion_requested_at is not null
    )
  );

drop policy if exists "application_packets_delete_own" on public.application_packets;

grant select on public.application_packets to authenticated;
revoke insert on public.application_packets from authenticated;
revoke delete on public.application_packets from authenticated;
revoke update on public.application_packets from authenticated;

create index if not exists application_packets_owner_updated_idx
  on public.application_packets (owner_id, updated_at desc);

create index if not exists application_packets_owner_job_idx
  on public.application_packets (owner_id, job_id, updated_at desc)
  where job_id is not null;

drop index if exists application_packets_owner_job_source_active_uidx;
create unique index if not exists application_packets_owner_job_source_active_uidx
  on public.application_packets (owner_id, job_id, source_cv_id)
  where archived_at is null
    and job_id is not null
    and source_cv_id is not null;

drop index if exists application_packets_owner_job_source_lineage_active_uidx;
drop index if exists application_packets_owner_job_source_lineage_version_uidx;
create unique index if not exists application_packets_owner_job_source_lineage_version_uidx
  on public.application_packets (
    owner_id, job_id, source_cv_id, source_cv_hash, job_content_hash,
    prompt_version, score_system_id, packet_version
  )
  where archived_at is null
    and job_id is not null
    and source_cv_id is not null
    and source_cv_hash is not null
    and job_content_hash is not null
    and prompt_version is not null
    and score_system_id is not null;
