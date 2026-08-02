-- Atomically preserve imported-job provenance and merge concurrent job
-- metadata updates. These are server-only boundaries: ownership is supplied
-- from the verified request identity and rechecked under profile/job locks.

create or replace function public.sanitize_saved_job_source_metadata(
  p_value jsonb
)
returns jsonb
language plpgsql
immutable
set search_path = ''
as $$
declare
  item record;
  result jsonb := '{}'::jsonb;
  serialized text;
  scalar_value text;
  value_type text;
begin
  if p_value is null or jsonb_typeof(p_value) <> 'object' then
    return result;
  end if;
  for item in select entry.key, entry.value from jsonb_each(p_value) as entry
  loop
    if item.key not in (
      'terms_basis', 'manual_verification_required', 'crawl_policy', 'origin',
      'site', 'date_posted', 'first_seen', 'normalized_role', 'source_db',
      'priority_rank', 'rank_bucket', 'rank_badge', 'fit_note', 'risk_note',
      'source_artifact', 'verified_at', 'revalidation', 'full_jd_status',
      'source_links', 'job_family', 'source_city', 'search_criteria',
      'match_reasons', 'broader_match', 'ranking', 'starred'
    ) then
      continue;
    end if;
    serialized := item.value::text;
    if octet_length(serialized) > 65536
       or serialized ~* (
         '(li_at|jsessionid|csrf|session[_-]?token|access[_-]?token|' ||
         'refresh[_-]?token|id[_-]?token|authorization|cookie|password|' ||
         'secret|api[_-]?key|access[_-]?key|private[_-]?key|' ||
         'client[_-]?secret|bearer)[[:space:]_"-]*[:=]|' ||
         'bearer[[:space:]]+[A-Za-z0-9._~+/=-]+|' ||
         'sk-(ant|proj|live|test|or|gsk|mistral|[A-Za-z0-9])' ||
         '[A-Za-z0-9_-]{8,}|AIza[0-9A-Za-z_-]{20,}|' ||
         'ya29\.[0-9A-Za-z_-]{20,}'
       ) then
      continue;
    end if;
    value_type := jsonb_typeof(item.value);
    if item.key in ('source_links', 'search_criteria', 'match_reasons', 'ranking') then
      if value_type in ('object', 'array', 'string', 'number', 'boolean') then
        result := result || jsonb_build_object(item.key, item.value);
      end if;
      continue;
    end if;
    if value_type = 'string' then
      scalar_value := item.value #>> '{}';
      -- Mirrors `_safe_source_metadata` in jobs_service.py / account_service.py:
      -- a source_db that looks like a filesystem path is dropped entirely.
      -- `strpos` rather than LIKE for the backslash: backslash is LIKE's default
      -- escape character, so `like '%\%'` tests for a trailing literal percent
      -- sign, not a backslash — it silently let Windows paths through while
      -- falsely dropping any value ending in '%'.
      if item.key = 'source_db'
         and (scalar_value like '%/%'
              or strpos(scalar_value, '\') > 0
              or scalar_value like '.%') then
        continue;
      end if;
      if item.key = 'source_artifact' then
        scalar_value := regexp_replace(scalar_value, '^.*[\\/]', '');
      end if;
      result := result || jsonb_build_object(item.key, left(scalar_value, 1000));
    elsif value_type in ('number', 'boolean') then
      result := result || jsonb_build_object(item.key, item.value);
    end if;
  end loop;
  return result;
end;
$$;

create or replace function public.sanitize_saved_job_source_metadata_trigger()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  new.source_metadata := public.sanitize_saved_job_source_metadata(
    new.source_metadata
  );
  return new;
end;
$$;

drop trigger if exists sanitize_saved_job_source_metadata_on_write
  on public.saved_jobs;
create trigger sanitize_saved_job_source_metadata_on_write
before insert or update of source_metadata on public.saved_jobs
for each row execute function public.sanitize_saved_job_source_metadata_trigger();

revoke update (source_metadata) on table public.saved_jobs from authenticated;

create or replace function public.save_import_result_as_job(
  p_owner_id uuid,
  p_run_id uuid,
  p_result_id uuid,
  p_job_payload jsonb,
  p_now timestamptz
)
returns jsonb
language plpgsql
security definer
set search_path = ''
set lock_timeout = '2s'
as $$
declare
  result_row public.job_import_results%rowtype;
  existing_job public.saved_jobs%rowtype;
  created_job public.saved_jobs%rowtype;
  payload_provider text := nullif(p_job_payload ->> 'source_provider', '');
  payload_external_id text := nullif(p_job_payload ->> 'source_external_id', '');
  payload_content_hash text := nullif(p_job_payload ->> 'content_hash', '');
begin
  if p_owner_id is null or p_run_id is null or p_result_id is null
     or p_job_payload is null or jsonb_typeof(p_job_payload) <> 'object'
     or pg_column_size(p_job_payload) > 262144 or p_now is null then
    raise exception 'invalid imported job save'
      using errcode = '22023';
  end if;

  perform p.id
    from public.profiles as p
   where p.id = p_owner_id
     and p.deletion_requested_at is null
   for share;
  if not found then
    return jsonb_build_object('status', 'blocked', 'code', 'account_unavailable');
  end if;

  perform r.id
    from public.job_import_runs as r
   where r.id = p_run_id
     and r.owner_id = p_owner_id
   for share;
  if not found then
    return jsonb_build_object('status', 'blocked', 'code', 'import_run_not_found');
  end if;

  select i.*
    into result_row
    from public.job_import_results as i
   where i.id = p_result_id
     and i.import_run_id = p_run_id
     and i.owner_id = p_owner_id
   for update;
  if result_row.id is null then
    return jsonb_build_object('status', 'blocked', 'code', 'import_result_not_found');
  end if;

  if result_row.saved_job_id is not null then
    select j.*
      into existing_job
      from public.saved_jobs as j
     where j.id = result_row.saved_job_id
       and j.owner_id = p_owner_id;
    if existing_job.id is not null then
      return jsonb_build_object(
        'status', 'linked',
        'job', to_jsonb(existing_job),
        'result_id', result_row.id
      );
    end if;
  end if;

  if payload_content_hash is not null
     and payload_content_hash !~ '^[0-9a-f]{64}$' then
    raise exception 'invalid imported job content hash'
      using errcode = '22023';
  end if;
  if p_job_payload ->> 'jd_text' is null
     or btrim(p_job_payload ->> 'jd_text') = ''
     or octet_length(p_job_payload ->> 'jd_text') > 20000 then
    raise exception 'invalid imported job description'
      using errcode = '22023';
  end if;
  if jsonb_typeof(coalesce(p_job_payload -> 'jd_json', '{}'::jsonb)) <> 'object'
     or jsonb_typeof(coalesce(p_job_payload -> 'source_metadata', '{}'::jsonb)) <> 'object'
     or jsonb_typeof(coalesce(p_job_payload -> 'experience_requirements', '{}'::jsonb)) <> 'object' then
    raise exception 'invalid imported job structured payload'
      using errcode = '22023';
  end if;

  -- Lock every duplicate identity used below. Taking external identity first
  -- and content identity second gives all callers one deterministic order.
  -- Two different external postings with identical content therefore cannot
  -- both pass the content-hash check and insert duplicate saved jobs.
  if payload_provider is not null and payload_external_id is not null then
    perform pg_catalog.pg_advisory_xact_lock(
      pg_catalog.hashtextextended(
        p_owner_id::text || ':external:' || payload_provider || ':' || payload_external_id,
        0
      )
    );
  end if;
  if payload_content_hash is not null then
    perform pg_catalog.pg_advisory_xact_lock(
      pg_catalog.hashtextextended(
        p_owner_id::text || ':content:' || payload_content_hash,
        0
      )
    );
  end if;
  if payload_external_id is null and payload_content_hash is null then
    perform pg_catalog.pg_advisory_xact_lock(
      pg_catalog.hashtextextended(
        p_owner_id::text || ':result:' || p_result_id::text,
        0
      )
    );
  end if;

  if payload_provider is not null and payload_external_id is not null then
    select j.*
      into existing_job
      from public.saved_jobs as j
     where j.owner_id = p_owner_id
       and j.source_provider = payload_provider
       and j.source_external_id = payload_external_id
     order by j.created_at, j.id
     limit 1
     for update;
  end if;
  if existing_job.id is null and payload_content_hash is not null then
    select j.*
      into existing_job
      from public.saved_jobs as j
     where j.owner_id = p_owner_id
       and j.content_hash = payload_content_hash
     order by j.created_at, j.id
     limit 1
     for update;
  end if;

  if existing_job.id is null then
    insert into public.saved_jobs (
      owner_id, import_run_id, source_provider, source_type,
      source_external_id, title, company, jd_text, jd_json, source_url,
      source_metadata, source_country, source_region, source_location,
      remote_mode, salary_text, seniority, verification_status, content_hash,
      experience_requirements, last_seen_at, expires_at, status, updated_at
    ) values (
      p_owner_id,
      p_run_id,
      payload_provider,
      nullif(p_job_payload ->> 'source_type', ''),
      payload_external_id,
      nullif(p_job_payload ->> 'title', ''),
      nullif(p_job_payload ->> 'company', ''),
      p_job_payload ->> 'jd_text',
      coalesce(p_job_payload -> 'jd_json', '{}'::jsonb),
      nullif(p_job_payload ->> 'source_url', ''),
      public.sanitize_saved_job_source_metadata(
        coalesce(p_job_payload -> 'source_metadata', '{}'::jsonb)
      ),
      nullif(p_job_payload ->> 'source_country', ''),
      nullif(p_job_payload ->> 'source_region', ''),
      nullif(p_job_payload ->> 'source_location', ''),
      nullif(p_job_payload ->> 'remote_mode', ''),
      nullif(p_job_payload ->> 'salary_text', ''),
      nullif(p_job_payload ->> 'seniority', ''),
      coalesce(nullif(p_job_payload ->> 'verification_status', ''), 'needs_review'),
      payload_content_hash,
      coalesce(p_job_payload -> 'experience_requirements', '{}'::jsonb),
      nullif(p_job_payload ->> 'last_seen_at', '')::timestamptz,
      nullif(p_job_payload ->> 'expires_at', '')::timestamptz,
      'saved',
      p_now
    )
    returning * into created_job;
  else
    created_job := existing_job;
  end if;

  update public.job_import_results as i
     set selected = true,
         saved_job_id = created_job.id,
         updated_at = p_now
   where i.id = result_row.id
     and i.owner_id = p_owner_id;

  return jsonb_build_object(
    'status', case when existing_job.id is null then 'created' else 'linked' end,
    'job', to_jsonb(created_job),
    'result_id', result_row.id
  );
end;
$$;

create or replace function public.set_saved_job_star(
  p_owner_id uuid,
  p_job_id uuid,
  p_starred boolean,
  p_now timestamptz
)
returns jsonb
language plpgsql
security definer
set search_path = ''
set lock_timeout = '2s'
as $$
declare
  updated_job public.saved_jobs%rowtype;
begin
  if p_owner_id is null or p_job_id is null or p_starred is null or p_now is null then
    raise exception 'invalid saved job star update'
      using errcode = '22023';
  end if;
  perform p.id
    from public.profiles as p
   where p.id = p_owner_id
     and p.deletion_requested_at is null
   for share;
  if not found then
    return jsonb_build_object('status', 'blocked', 'code', 'account_unavailable');
  end if;
  update public.saved_jobs as j
     set source_metadata = pg_catalog.jsonb_set(
           public.sanitize_saved_job_source_metadata(j.source_metadata),
           '{starred}',
           to_jsonb(p_starred),
           true
         ),
         updated_at = p_now
   where j.id = p_job_id
     and j.owner_id = p_owner_id
  returning * into updated_job;
  if updated_job.id is null then
    return jsonb_build_object('status', 'not_found', 'code', 'job_not_found');
  end if;
  return jsonb_build_object('status', 'saved', 'job', to_jsonb(updated_job));
end;
$$;

create or replace function public.revalidate_saved_job_source(
  p_owner_id uuid,
  p_job_id uuid,
  p_mark_stale boolean,
  p_full_jd_text text,
  p_jd_json jsonb,
  p_content_hash text,
  p_experience_requirements jsonb,
  p_now timestamptz
)
returns jsonb
language plpgsql
security definer
set search_path = ''
set lock_timeout = '2s'
as $$
declare
  locked_job public.saved_jobs%rowtype;
  updated_job public.saved_jobs%rowtype;
  merged_metadata jsonb;
  next_verification text;
begin
  if p_owner_id is null or p_job_id is null or p_mark_stale is null or p_now is null
     or (p_full_jd_text is not null and octet_length(p_full_jd_text) > 20000)
     or (p_jd_json is not null and jsonb_typeof(p_jd_json) <> 'object')
     or (p_experience_requirements is not null
         and jsonb_typeof(p_experience_requirements) <> 'object')
     or (p_content_hash is not null and p_content_hash !~ '^[0-9a-f]{64}$') then
    raise exception 'invalid saved job source update'
      using errcode = '22023';
  end if;
  perform p.id
    from public.profiles as p
   where p.id = p_owner_id
     and p.deletion_requested_at is null
   for share;
  if not found then
    return jsonb_build_object('status', 'blocked', 'code', 'account_unavailable');
  end if;
  select j.*
    into locked_job
    from public.saved_jobs as j
   where j.id = p_job_id
     and j.owner_id = p_owner_id
   for update;
  if locked_job.id is null then
    return jsonb_build_object('status', 'not_found', 'code', 'job_not_found');
  end if;

  merged_metadata := public.sanitize_saved_job_source_metadata(
    locked_job.source_metadata
  );
  merged_metadata := pg_catalog.jsonb_set(
    merged_metadata, '{revalidation}', to_jsonb('manual_review'::text), true
  );
  if p_mark_stale then
    next_verification := 'stale';
  else
    next_verification := case
      when p_full_jd_text is null and merged_metadata ->> 'full_jd_status' = 'missing'
        then 'needs_review'
      else 'user_provided'
    end;
    merged_metadata := pg_catalog.jsonb_set(
      merged_metadata, '{verified_at}', to_jsonb(p_now::date::text), true
    );
  end if;
  if p_full_jd_text is not null then
    merged_metadata := pg_catalog.jsonb_set(
      merged_metadata, '{full_jd_status}', to_jsonb('reviewed'::text), true
    );
  end if;

  update public.saved_jobs as j
     set verification_status = next_verification,
         last_seen_at = case when p_mark_stale then j.last_seen_at else p_now end,
         jd_text = coalesce(p_full_jd_text, j.jd_text),
         jd_json = coalesce(p_jd_json, j.jd_json),
         content_hash = coalesce(p_content_hash, j.content_hash),
         experience_requirements = coalesce(
           p_experience_requirements, j.experience_requirements
         ),
         source_metadata = merged_metadata,
         updated_at = p_now
   where j.id = locked_job.id
     and j.owner_id = p_owner_id
  returning * into updated_job;

  return jsonb_build_object('status', 'saved', 'job', to_jsonb(updated_job));
end;
$$;

revoke execute on function public.save_import_result_as_job(
  uuid, uuid, uuid, jsonb, timestamptz
) from public, anon, authenticated;
grant execute on function public.save_import_result_as_job(
  uuid, uuid, uuid, jsonb, timestamptz
) to service_role;

revoke execute on function public.set_saved_job_star(
  uuid, uuid, boolean, timestamptz
) from public, anon, authenticated;
grant execute on function public.set_saved_job_star(
  uuid, uuid, boolean, timestamptz
) to service_role;

revoke execute on function public.revalidate_saved_job_source(
  uuid, uuid, boolean, text, jsonb, text, jsonb, timestamptz
) from public, anon, authenticated;
grant execute on function public.revalidate_saved_job_source(
  uuid, uuid, boolean, text, jsonb, text, jsonb, timestamptz
) to service_role;

revoke execute on function public.sanitize_saved_job_source_metadata(jsonb)
  from public, anon, authenticated;
revoke execute on function public.sanitize_saved_job_source_metadata_trigger()
  from public, anon, authenticated;
