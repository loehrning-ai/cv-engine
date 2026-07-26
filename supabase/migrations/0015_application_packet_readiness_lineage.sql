-- OnePager — additive packet readiness and lineage hardening for applied DBs.

alter table public.application_packets
  add column if not exists score_system_id text,
  add column if not exists profile_answers_complete boolean not null default false,
  add column if not exists legal_answers_confirmed boolean not null default false,
  add column if not exists visa_answers_confirmed boolean not null default false,
  add column if not exists cover_letter_review_status text not null default 'pending',
  add column if not exists cover_letter_review_confirmed_at timestamptz,
  add column if not exists cover_letter_review_id text;

update public.application_packets
set score_system_id = 'semantic_fit'
where score_system_id is null or btrim(score_system_id) = '';

alter table public.application_packets
  alter column score_system_id set default 'semantic_fit',
  alter column score_system_id set not null;

alter table public.application_packets drop constraint if exists application_packets_score_system_check;
alter table public.application_packets add constraint application_packets_score_system_check
  check (btrim(score_system_id) <> '');

alter table public.application_packets drop constraint if exists application_packets_cover_letter_review_status_check;
alter table public.application_packets add constraint application_packets_cover_letter_review_status_check
  check (cover_letter_review_status in ('pending', 'confirmed'));

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

revoke update on public.application_packets from authenticated;
