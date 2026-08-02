-- OnePager -- read-only release/schema compatibility contract.
--
-- Deployment automation calls this RPC with the service role before starting
-- an application image. It exposes only a fixed release identifier, integer
-- versions, and aggregate booleans. It never accepts or returns user identity,
-- request claims, secrets, relation contents, or per-function ACL details.

create or replace function public.release_schema_contract()
returns table (
  contract_id text,
  contract_version integer,
  worker_contract_version integer,
  worker_contract_ok boolean,
  authenticated_rpc_execute_ok boolean,
  evidence_backed_job_status_ok boolean,
  compatible boolean
)
language sql
stable
security definer
set search_path = ''
as $$
  with worker_contract as (
    select public.account_deletion_worker_contract_version() as version
  ),
  authenticated_runtime_rpcs(signature) as (
    values
      ('public.cancel_account_deletion()'),
      ('public.consume_managed_credit(text)'),
      ('public.delete_document_safely(uuid,text)'),
      ('public.delete_saved_job_safely(uuid)'),
      ('public.insert_document_if_account_active(text,text,text,uuid,text,text,text,text,text)'),
      ('public.mark_first_cv_seen()'),
      ('public.record_byo_usage(text,text)'),
      ('public.request_account_deletion_with_artifact_detach()'),
      ('public.seed_first_cv_template(text,text,text,text,text)')
  ),
  authenticated_runtime_contract as (
    select pg_catalog.bool_and(
      pg_catalog.to_regprocedure(signature) is not null
      and coalesce(
        pg_catalog.has_function_privilege(
          'authenticated',
          pg_catalog.to_regprocedure(signature),
          'EXECUTE'
        ),
        false
      )
    ) as execute_ok
    from authenticated_runtime_rpcs
  ),
  evidence_backed_job_status_contract as (
    select coalesce(
      pg_catalog.bool_or(
        trigger.tgname = 'reject_evidence_free_applied_job_status_on_write'
        and trigger.tgrelid = pg_catalog.to_regclass('public.saved_jobs')
        and trigger.tgfoid = pg_catalog.to_regprocedure(
          'public.reject_evidence_free_applied_job_status()'
        )
        and trigger.tgenabled in ('O', 'A')
        and not trigger.tgisinternal
      ),
      false
    ) as enabled
    from pg_catalog.pg_trigger as trigger
  )
  select
    'onepager-20260801170000-v1'::text,
    1,
    worker_contract.version,
    worker_contract.version = 2,
    authenticated_runtime_contract.execute_ok,
    evidence_backed_job_status_contract.enabled,
    worker_contract.version = 2
      and authenticated_runtime_contract.execute_ok
      and evidence_backed_job_status_contract.enabled
  from worker_contract
  cross join authenticated_runtime_contract
  cross join evidence_backed_job_status_contract;
$$;

comment on function public.release_schema_contract() is
  'Service-only, read-only release compatibility probe. Returns aggregate schema and RPC privilege compatibility without tenant data or request credentials.';

revoke execute on function public.release_schema_contract()
  from public, anon, authenticated, service_role;
grant execute on function public.release_schema_contract()
  to service_role;
