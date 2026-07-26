-- OnePager — move import provenance trust hardening out of historical 0010.

update public.job_import_results
set source_provider = 'other', updated_at = now()
where source_provider = 'approved_adapter';

update public.saved_jobs
set source_provider = 'other', updated_at = now()
where source_provider = 'approved_adapter';

update public.job_import_results
set source_type = 'csv_import', updated_at = now()
where source_type = 'approved_adapter';

update public.saved_jobs
set source_type = 'csv_import', updated_at = now()
where source_type = 'approved_adapter';

update public.job_import_results
set verification_status = 'needs_review', updated_at = now()
where verification_status = 'verified';

update public.saved_jobs
set verification_status = 'needs_review', updated_at = now()
where verification_status = 'verified';

alter table public.job_import_results drop constraint if exists job_import_results_source_provider_check;
alter table public.job_import_results add constraint job_import_results_source_provider_check
  check (source_provider in ('manual', 'linkedin', 'other'));

alter table public.job_import_results drop constraint if exists job_import_results_source_type_check;
alter table public.job_import_results add constraint job_import_results_source_type_check
  check (source_type in ('manual_paste', 'manual_url_reference', 'csv_import'));

alter table public.job_import_results drop constraint if exists job_import_results_verification_status_check;
alter table public.job_import_results add constraint job_import_results_verification_status_check
  check (verification_status in ('user_provided', 'needs_review', 'stale'));

alter table public.saved_jobs drop constraint if exists saved_jobs_source_provider_check;
alter table public.saved_jobs add constraint saved_jobs_source_provider_check
  check (source_provider is null or source_provider in ('manual', 'linkedin', 'other'));

alter table public.saved_jobs drop constraint if exists saved_jobs_source_type_check;
alter table public.saved_jobs add constraint saved_jobs_source_type_check
  check (source_type is null or source_type in ('manual_paste', 'manual_url_reference', 'csv_import'));

alter table public.saved_jobs drop constraint if exists saved_jobs_verification_status_check;
alter table public.saved_jobs add constraint saved_jobs_verification_status_check
  check (verification_status in ('user_provided', 'needs_review', 'stale'));

revoke insert on public.job_import_results from authenticated;
revoke update on public.job_import_results from authenticated;
grant select on public.job_import_results to authenticated;
