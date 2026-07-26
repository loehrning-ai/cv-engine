-- CV Copilot — enforce tenant ownership on every denormalized relationship.
--
-- RLS protects browser writes, but backend service-role writes bypass RLS.
-- Composite foreign keys make cross-owner links impossible at the relational
-- layer as well. Abort instead of silently rewriting any inconsistent data.

do $preflight$
begin
  if exists (
    select 1
      from public.documents as c
      join public.saved_jobs as p on p.id = c.job_id
     where c.owner_id <> p.owner_id
  ) or exists (
    select 1
      from public.pdf_artifacts as c
      join public.documents as p on p.id = c.document_id
     where c.owner_id <> p.owner_id
  ) or exists (
    select 1
      from public.saved_jobs as c
      join public.job_import_runs as p on p.id = c.import_run_id
     where c.owner_id <> p.owner_id
  ) or exists (
    select 1
      from public.job_import_results as c
      join public.job_import_runs as p on p.id = c.import_run_id
     where c.owner_id <> p.owner_id
  ) or exists (
    select 1
      from public.job_import_results as c
      join public.saved_jobs as p on p.id = c.saved_job_id
     where c.owner_id <> p.owner_id
  ) or exists (
    select 1
      from public.application_packets as c
      join public.saved_jobs as p on p.id = c.job_id
     where c.owner_id <> p.owner_id
  ) or exists (
    select 1
      from public.application_packets as c
      join public.documents as p on p.id = c.source_cv_id
     where c.owner_id <> p.owner_id
  ) or exists (
    select 1
      from public.application_packets as c
      join public.documents as p on p.id = c.tailored_cv_id
     where c.owner_id <> p.owner_id
  ) or exists (
    select 1
      from public.application_packets as c
      join public.documents as p on p.id = c.cover_letter_id
     where c.owner_id <> p.owner_id
  ) or exists (
    select 1
      from public.application_apply_attempts as c
      join public.saved_jobs as p on p.id = c.job_id
     where c.owner_id <> p.owner_id
  ) or exists (
    select 1
      from public.application_apply_attempts as c
      join public.application_packets as p on p.id = c.packet_id
     where c.owner_id <> p.owner_id
  ) or exists (
    select 1
      from public.tailor_review_states as c
      join public.documents as p on p.id = c.cv_id
     where c.owner_id <> p.owner_id
  ) or exists (
    select 1
      from public.tailor_review_states as c
      join public.saved_jobs as p on p.id = c.job_id
     where c.owner_id <> p.owner_id
  ) then
    raise exception 'cross-owner relationship detected; migration aborted'
      using errcode = '23514';
  end if;
end
$preflight$;

-- Parent keys referenced by the composite foreign keys.
create unique index if not exists documents_id_owner_uidx
  on public.documents (id, owner_id);
create unique index if not exists saved_jobs_id_owner_uidx
  on public.saved_jobs (id, owner_id);
create unique index if not exists job_import_runs_id_owner_uidx
  on public.job_import_runs (id, owner_id);
create unique index if not exists application_packets_id_owner_uidx
  on public.application_packets (id, owner_id);

-- Child-side indexes keep parent updates/deletes and validation bounded.
create index if not exists documents_job_owner_idx
  on public.documents (job_id, owner_id)
  where job_id is not null;
create index if not exists pdf_artifacts_document_owner_idx
  on public.pdf_artifacts (document_id, owner_id);
create index if not exists saved_jobs_import_run_owner_idx
  on public.saved_jobs (import_run_id, owner_id)
  where import_run_id is not null;
create index if not exists job_import_results_run_owner_idx
  on public.job_import_results (import_run_id, owner_id);
create index if not exists job_import_results_saved_job_owner_idx
  on public.job_import_results (saved_job_id, owner_id)
  where saved_job_id is not null;
create index if not exists application_packets_job_owner_idx
  on public.application_packets (job_id, owner_id)
  where job_id is not null;
create index if not exists application_packets_source_cv_owner_idx
  on public.application_packets (source_cv_id, owner_id)
  where source_cv_id is not null;
create index if not exists application_packets_tailored_cv_owner_idx
  on public.application_packets (tailored_cv_id, owner_id)
  where tailored_cv_id is not null;
create index if not exists application_packets_cover_letter_owner_idx
  on public.application_packets (cover_letter_id, owner_id)
  where cover_letter_id is not null;
create index if not exists application_apply_attempts_job_owner_idx
  on public.application_apply_attempts (job_id, owner_id)
  where job_id is not null;
create index if not exists application_apply_attempts_packet_owner_idx
  on public.application_apply_attempts (packet_id, owner_id)
  where packet_id is not null;
create index if not exists tailor_review_states_cv_owner_idx
  on public.tailor_review_states (cv_id, owner_id)
  where cv_id is not null;
create index if not exists tailor_review_states_job_owner_idx
  on public.tailor_review_states (job_id, owner_id)
  where job_id is not null;

do $constraints$
begin
  if not exists (
    select 1 from pg_constraint
     where conrelid = 'public.documents'::regclass
       and conname = 'documents_job_owner_fkey'
  ) then
    alter table public.documents
      add constraint documents_job_owner_fkey
      foreign key (job_id, owner_id)
      references public.saved_jobs (id, owner_id)
      on delete set null (job_id)
      not valid;
  end if;

  if not exists (
    select 1 from pg_constraint
     where conrelid = 'public.pdf_artifacts'::regclass
       and conname = 'pdf_artifacts_document_owner_fkey'
  ) then
    alter table public.pdf_artifacts
      add constraint pdf_artifacts_document_owner_fkey
      foreign key (document_id, owner_id)
      references public.documents (id, owner_id)
      on delete cascade
      not valid;
  end if;

  if not exists (
    select 1 from pg_constraint
     where conrelid = 'public.saved_jobs'::regclass
       and conname = 'saved_jobs_import_run_owner_fkey'
  ) then
    alter table public.saved_jobs
      add constraint saved_jobs_import_run_owner_fkey
      foreign key (import_run_id, owner_id)
      references public.job_import_runs (id, owner_id)
      on delete set null (import_run_id)
      not valid;
  end if;

  if not exists (
    select 1 from pg_constraint
     where conrelid = 'public.job_import_results'::regclass
       and conname = 'job_import_results_run_owner_fkey'
  ) then
    alter table public.job_import_results
      add constraint job_import_results_run_owner_fkey
      foreign key (import_run_id, owner_id)
      references public.job_import_runs (id, owner_id)
      on delete cascade
      not valid;
  end if;

  if not exists (
    select 1 from pg_constraint
     where conrelid = 'public.job_import_results'::regclass
       and conname = 'job_import_results_saved_job_owner_fkey'
  ) then
    alter table public.job_import_results
      add constraint job_import_results_saved_job_owner_fkey
      foreign key (saved_job_id, owner_id)
      references public.saved_jobs (id, owner_id)
      on delete set null (saved_job_id)
      not valid;
  end if;

  if not exists (
    select 1 from pg_constraint
     where conrelid = 'public.application_packets'::regclass
       and conname = 'application_packets_job_owner_fkey'
  ) then
    alter table public.application_packets
      add constraint application_packets_job_owner_fkey
      foreign key (job_id, owner_id)
      references public.saved_jobs (id, owner_id)
      on delete set null (job_id)
      not valid;
  end if;

  if not exists (
    select 1 from pg_constraint
     where conrelid = 'public.application_packets'::regclass
       and conname = 'application_packets_source_cv_owner_fkey'
  ) then
    alter table public.application_packets
      add constraint application_packets_source_cv_owner_fkey
      foreign key (source_cv_id, owner_id)
      references public.documents (id, owner_id)
      on delete set null (source_cv_id)
      not valid;
  end if;

  if not exists (
    select 1 from pg_constraint
     where conrelid = 'public.application_packets'::regclass
       and conname = 'application_packets_tailored_cv_owner_fkey'
  ) then
    alter table public.application_packets
      add constraint application_packets_tailored_cv_owner_fkey
      foreign key (tailored_cv_id, owner_id)
      references public.documents (id, owner_id)
      on delete set null (tailored_cv_id)
      not valid;
  end if;

  if not exists (
    select 1 from pg_constraint
     where conrelid = 'public.application_packets'::regclass
       and conname = 'application_packets_cover_letter_owner_fkey'
  ) then
    alter table public.application_packets
      add constraint application_packets_cover_letter_owner_fkey
      foreign key (cover_letter_id, owner_id)
      references public.documents (id, owner_id)
      on delete set null (cover_letter_id)
      not valid;
  end if;

  if not exists (
    select 1 from pg_constraint
     where conrelid = 'public.application_apply_attempts'::regclass
       and conname = 'application_apply_attempts_job_owner_fkey'
  ) then
    alter table public.application_apply_attempts
      add constraint application_apply_attempts_job_owner_fkey
      foreign key (job_id, owner_id)
      references public.saved_jobs (id, owner_id)
      on delete set null (job_id)
      not valid;
  end if;

  if not exists (
    select 1 from pg_constraint
     where conrelid = 'public.application_apply_attempts'::regclass
       and conname = 'application_apply_attempts_packet_owner_fkey'
  ) then
    alter table public.application_apply_attempts
      add constraint application_apply_attempts_packet_owner_fkey
      foreign key (packet_id, owner_id)
      references public.application_packets (id, owner_id)
      on delete set null (packet_id)
      not valid;
  end if;

  if not exists (
    select 1 from pg_constraint
     where conrelid = 'public.tailor_review_states'::regclass
       and conname = 'tailor_review_states_cv_owner_fkey'
  ) then
    alter table public.tailor_review_states
      add constraint tailor_review_states_cv_owner_fkey
      foreign key (cv_id, owner_id)
      references public.documents (id, owner_id)
      on delete cascade
      not valid;
  end if;

  if not exists (
    select 1 from pg_constraint
     where conrelid = 'public.tailor_review_states'::regclass
       and conname = 'tailor_review_states_job_owner_fkey'
  ) then
    alter table public.tailor_review_states
      add constraint tailor_review_states_job_owner_fkey
      foreign key (job_id, owner_id)
      references public.saved_jobs (id, owner_id)
      on delete set null (job_id)
      not valid;
  end if;
end
$constraints$;

alter table public.documents
  validate constraint documents_job_owner_fkey;
alter table public.pdf_artifacts
  validate constraint pdf_artifacts_document_owner_fkey;
alter table public.saved_jobs
  validate constraint saved_jobs_import_run_owner_fkey;
alter table public.job_import_results
  validate constraint job_import_results_run_owner_fkey;
alter table public.job_import_results
  validate constraint job_import_results_saved_job_owner_fkey;
alter table public.application_packets
  validate constraint application_packets_job_owner_fkey;
alter table public.application_packets
  validate constraint application_packets_source_cv_owner_fkey;
alter table public.application_packets
  validate constraint application_packets_tailored_cv_owner_fkey;
alter table public.application_packets
  validate constraint application_packets_cover_letter_owner_fkey;
alter table public.application_apply_attempts
  validate constraint application_apply_attempts_job_owner_fkey;
alter table public.application_apply_attempts
  validate constraint application_apply_attempts_packet_owner_fkey;
alter table public.tailor_review_states
  validate constraint tailor_review_states_cv_owner_fkey;
alter table public.tailor_review_states
  validate constraint tailor_review_states_job_owner_fkey;
