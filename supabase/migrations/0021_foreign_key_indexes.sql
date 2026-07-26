-- CV Copilot — cover every active foreign key with a leading-column index.
--
-- These indexes bound parent-row updates/deletes and keep workflow joins from
-- degrading as per-user history grows.

create index if not exists application_apply_attempts_job_id_idx
  on public.application_apply_attempts (job_id);
create index if not exists application_apply_attempts_packet_id_idx
  on public.application_apply_attempts (packet_id);

create index if not exists application_packets_cover_letter_id_idx
  on public.application_packets (cover_letter_id);
create index if not exists application_packets_job_id_idx
  on public.application_packets (job_id);
create index if not exists application_packets_source_cv_id_idx
  on public.application_packets (source_cv_id);
create index if not exists application_packets_tailored_cv_id_idx
  on public.application_packets (tailored_cv_id);

create index if not exists job_import_results_import_run_id_idx
  on public.job_import_results (import_run_id);
create index if not exists job_import_results_saved_job_id_idx
  on public.job_import_results (saved_job_id);
create index if not exists saved_jobs_import_run_id_idx
  on public.saved_jobs (import_run_id);

create index if not exists tailor_review_states_cv_id_idx
  on public.tailor_review_states (cv_id);
create index if not exists tailor_review_states_job_id_idx
  on public.tailor_review_states (job_id);
