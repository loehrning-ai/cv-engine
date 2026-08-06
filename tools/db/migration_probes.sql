-- Behavioural probes for the database guarantees.
--
-- The Python suite asserts these guarantees by matching text in the .sql files,
-- which proves the migration SAYS the right thing, not that PostgreSQL DOES it.
-- This file EXECUTES them against a real, freshly migrated database. It raises
-- on the first failed guarantee so the caller's exit code is meaningful.
--
-- Run via tools/db/replay_migrations.sh — it provisions the disposable cluster
-- this script expects (Supabase role shim + auth.uid()).

set client_min_messages = notice;

insert into auth.users (id)
values ('00000000-0000-4000-8000-000000000001')
on conflict do nothing;

do $probes$
declare
  owner constant uuid := '00000000-0000-4000-8000-000000000001';
  failures text[] := '{}';
  checks int := 0;
  jid uuid;
  run_id uuid;
  res jsonb;
  stored jsonb;
  state text;
  msg text;
  ok boolean;

  procedure_note text;
begin
  perform set_config('request.jwt.claim.sub', owner::text, false);

  ---------------------------------------------------------------------------
  -- Evidence-backed applied state: a mutable label can never claim an
  -- external application attempt.
  ---------------------------------------------------------------------------
  checks := checks + 1;
  ok := false;
  begin
    insert into public.saved_jobs (owner_id, title, status)
    values (owner, 'probe-insert-applied', 'applied');
  exception when others then
    get stacked diagnostics state = returned_sqlstate, msg = message_text;
    ok := (state = '23514' and msg like '%application evidence%');
  end;
  if not ok then
    failures := array_append(failures, 'INSERT status=applied was not rejected with 23514');
  end if;

  insert into public.saved_jobs (owner_id, title, status)
  values (owner, 'probe-update-applied', 'open')
  returning id into jid;

  checks := checks + 1;
  ok := false;
  begin
    update public.saved_jobs set status = 'applied' where id = jid;
  exception when others then
    get stacked diagnostics state = returned_sqlstate;
    ok := (state = '23514');
  end;
  if not ok then
    failures := array_append(failures, 'UPDATE to status=applied was not rejected with 23514');
  end if;

  checks := checks + 1;
  ok := true;
  begin
    update public.saved_jobs set status = 'closed' where id = jid;
  exception when others then
    ok := false;
  end;
  if not ok then
    failures := array_append(failures, 'legitimate lifecycle update open->closed was rejected');
  end if;

  -- Historical rows stay readable AND correctable to an honest state.
  checks := checks + 1;
  alter table public.saved_jobs
    disable trigger reject_evidence_free_applied_job_status_on_write;
  insert into public.saved_jobs (owner_id, title, status)
  values (owner, 'probe-historical-applied', 'applied')
  returning id into jid;
  alter table public.saved_jobs
    enable trigger reject_evidence_free_applied_job_status_on_write;
  ok := true;
  begin
    update public.saved_jobs set status = 'closed' where id = jid;
  exception when others then
    ok := false;
  end;
  if not ok then
    failures := array_append(failures, 'historical applied row could not be corrected');
  end if;

  ---------------------------------------------------------------------------
  -- The trigger function must not be an escalation vector.
  ---------------------------------------------------------------------------
  checks := checks + 1;
  select p.prosecdef = false
     and array_to_string(p.proconfig, ',') like '%search_path=%'
    into ok
    from pg_proc p
    join pg_namespace n on n.oid = p.pronamespace
   where n.nspname = 'public'
     and p.proname = 'reject_evidence_free_applied_job_status';
  if not coalesce(ok, false) then
    failures := array_append(failures, 'trigger fn is SECURITY DEFINER or does not pin search_path');
  end if;

  checks := checks + 1;
  select not (
      has_function_privilege('authenticated',
        'public.reject_evidence_free_applied_job_status()', 'execute')
      or has_function_privilege('anon',
        'public.reject_evidence_free_applied_job_status()', 'execute')
    ) into ok;
  if not ok then
    failures := array_append(failures, 'trigger fn execute is not revoked from anon/authenticated');
  end if;

  ---------------------------------------------------------------------------
  -- Application history is protected by RPC, not by client-side DELETE.
  ---------------------------------------------------------------------------
  checks := checks + 1;
  if has_table_privilege('authenticated', 'public.saved_jobs', 'delete') then
    failures := array_append(failures, 'authenticated still holds direct DELETE on saved_jobs');
  end if;

  checks := checks + 1;
  if has_table_privilege('authenticated', 'public.documents', 'delete') then
    failures := array_append(failures, 'authenticated still holds direct DELETE on documents');
  end if;

  checks := checks + 1;
  if has_function_privilege('authenticated',
       'public.release_schema_contract()', 'execute') then
    failures := array_append(failures, 'release_schema_contract() is callable by authenticated');
  end if;

  ---------------------------------------------------------------------------
  -- Import provenance is NOT application history: the saved_job_id FK is
  -- ON DELETE SET NULL, so the import record survives and the job stays
  -- deletable. Blocking here would make imported jobs undeletable forever.
  ---------------------------------------------------------------------------
  insert into public.saved_jobs (owner_id, title, status)
  values (owner, 'probe-imported-job', 'saved')
  returning id into jid;
  insert into public.job_import_runs (owner_id, provider)
  values (owner, 'manual')
  returning id into run_id;
  insert into public.job_import_results (
    owner_id, import_run_id, source_provider, source_type, jd_text, saved_job_id)
  values (owner, run_id, 'manual', 'manual_paste', 'jd body', jid);

  checks := checks + 1;
  select public.delete_saved_job_safely(jid) into res;
  if coalesce(res ->> 'status', '') <> 'deleted' then
    failures := array_append(failures, format('import-provenance-only job was not deletable: %s', res));
  end if;

  checks := checks + 1;
  select i.saved_job_id is null into ok
    from public.job_import_results i
   where i.import_run_id = run_id;
  if not coalesce(ok, false) then
    failures := array_append(failures, 'import provenance row did not survive with a null link');
  end if;

  -- A job that DOES carry application history must still be protected.
  insert into public.saved_jobs (owner_id, title, status)
  values (owner, 'probe-linked-job', 'saved')
  returning id into jid;
  insert into public.documents (owner_id, type, name, job_id)
  values (owner, 'cv', 'linked cv', jid);

  checks := checks + 1;
  select public.delete_saved_job_safely(jid) into res;
  if coalesce(res ->> 'code', '') <> 'job_has_application_history' then
    failures := array_append(failures, format('job with a linked document was not protected: %s', res));
  end if;

  ---------------------------------------------------------------------------
  -- source_db sanitizer: a filesystem path must be dropped on the direct
  -- PostgREST write path, where the Flask sanitizer is bypassed. Backslash is
  -- LIKE's default escape character, so `like '%\%'` silently let Windows
  -- paths through.
  ---------------------------------------------------------------------------
  checks := checks + 1;
  insert into public.saved_jobs (owner_id, title, source_metadata)
  values (owner, 'probe-windows-path',
          jsonb_build_object('source_db', 'C:\Users\victim\jobs.db'))
  returning source_metadata into stored;
  if stored ? 'source_db' then
    failures := array_append(failures, format('windows source_db path survived the sanitizer: %s', stored));
  end if;

  checks := checks + 1;
  insert into public.saved_jobs (owner_id, title, source_metadata)
  values (owner, 'probe-posix-path',
          jsonb_build_object('source_db', '/etc/passwd'))
  returning source_metadata into stored;
  if stored ? 'source_db' then
    failures := array_append(failures, format('posix source_db path survived the sanitizer: %s', stored));
  end if;

  -- A legitimate label must NOT be dropped (the old guard ate any value
  -- ending in '%').
  checks := checks + 1;
  insert into public.saved_jobs (owner_id, title, source_metadata)
  values (owner, 'probe-plain-label',
          jsonb_build_object('source_db', 'careers-export-100%'))
  returning source_metadata into stored;
  if not (stored ? 'source_db') then
    failures := array_append(failures, 'a plain source_db label was incorrectly dropped');
  end if;

  ---------------------------------------------------------------------------
  -- A failing cleanup row must back off, not spin. Before the backoff landed,
  -- record_pdf_artifact_cleanup_failure bumped attempt_count but left
  -- not_before alone, so the worker re-claimed the same doomed row every cycle.
  ---------------------------------------------------------------------------
  declare
    cleanup_id uuid;
    token constant uuid := '00000000-0000-4000-8000-0000000000ff';
    delay_first interval;
    delay_later interval;
  begin
    insert into public.pdf_artifact_cleanup_queue (
      owner_id, document_id, r2_key, content_hash, claim_token, claimed_at)
    values (
      owner,
      '00000000-0000-4000-8000-0000000000aa',
      'probe/owner/doc.pdf',
      repeat('a', 64),
      token,
      now())
    returning id into cleanup_id;

    checks := checks + 1;
    if not public.record_pdf_artifact_cleanup_failure(owner, cleanup_id, token) then
      failures := array_append(failures, 'failure accounting did not update the queue row');
    end if;

    select q.not_before - now() into delay_first
      from public.pdf_artifact_cleanup_queue q where q.id = cleanup_id;

    checks := checks + 1;
    if delay_first is null or delay_first <= interval '0' then
      failures := array_append(failures,
        format('first failure did not defer the row: %s', delay_first));
    end if;

    -- Simulate a row that has already failed repeatedly; the delay must grow.
    update public.pdf_artifact_cleanup_queue
       set attempt_count = 4, claim_token = token, claimed_at = now()
     where id = cleanup_id;
    perform public.record_pdf_artifact_cleanup_failure(owner, cleanup_id, token);
    select q.not_before - now() into delay_later
      from public.pdf_artifact_cleanup_queue q where q.id = cleanup_id;

    checks := checks + 1;
    if delay_later is null or delay_later <= delay_first then
      failures := array_append(failures,
        format('backoff did not grow with attempts: first=%s later=%s',
               delay_first, delay_later));
    end if;

    -- Capped, so a long-failing row still retries hourly rather than never.
    update public.pdf_artifact_cleanup_queue
       set attempt_count = 40, claim_token = token, claimed_at = now()
     where id = cleanup_id;
    perform public.record_pdf_artifact_cleanup_failure(owner, cleanup_id, token);

    checks := checks + 1;
    select q.not_before - now() into delay_later
      from public.pdf_artifact_cleanup_queue q where q.id = cleanup_id;
    if delay_later > interval '1 hour' + interval '1 minute' then
      failures := array_append(failures,
        format('backoff is unbounded: %s', delay_later));
    end if;

    -- The row must survive: it exists because an object still needs deleting.
    checks := checks + 1;
    if not exists (select 1 from public.pdf_artifact_cleanup_queue where id = cleanup_id) then
      failures := array_append(failures, 'a failing cleanup row was discarded');
    end if;
  end;

  ---------------------------------------------------------------------------
  -- Every public table carries RLS.
  ---------------------------------------------------------------------------
  checks := checks + 1;
  select string_agg(c.relname, ', ')
    into procedure_note
    from pg_class c
    join pg_namespace n on n.oid = c.relnamespace
   where n.nspname = 'public'
     and c.relkind = 'r'
     and c.relrowsecurity = false;
  if procedure_note is not null then
    failures := array_append(failures, format('public tables without RLS: %s', procedure_note));
  end if;

  ---------------------------------------------------------------------------
  if array_length(failures, 1) > 0 then
    raise exception E'% of % migration probes FAILED:\n  - %',
      array_length(failures, 1), checks, array_to_string(failures, E'\n  - ');
  end if;
  raise notice 'all % migration probes passed', checks;
end
$probes$;
