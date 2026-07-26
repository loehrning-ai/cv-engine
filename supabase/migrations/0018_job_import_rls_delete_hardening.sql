-- OnePager — forward-only import RLS/delete hardening.

drop policy if exists "job_import_runs_select_own" on public.job_import_runs;
create policy "job_import_runs_select_own" on public.job_import_runs
  for select to authenticated
  using (
    owner_id = (select auth.uid())
    and not exists (
      select 1 from public.profiles p
      where p.id = (select auth.uid())
        and p.deletion_requested_at is not null
    )
  );

drop policy if exists "job_import_results_select_own" on public.job_import_results;
create policy "job_import_results_select_own" on public.job_import_results
  for select to authenticated
  using (
    owner_id = (select auth.uid())
    and not exists (
      select 1 from public.profiles p
      where p.id = (select auth.uid())
        and p.deletion_requested_at is not null
    )
  );

drop policy if exists "job_import_runs_delete_own" on public.job_import_runs;
drop policy if exists "job_import_runs_insert_own" on public.job_import_runs;
drop policy if exists "job_import_runs_update_own" on public.job_import_runs;
drop policy if exists "job_import_results_delete_own" on public.job_import_results;

revoke insert on public.job_import_runs from authenticated;
revoke update on public.job_import_runs from authenticated;
revoke delete on public.job_import_runs from authenticated;
revoke delete on public.job_import_results from authenticated;
