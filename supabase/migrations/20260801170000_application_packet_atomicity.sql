-- Make the final cover-review transition and standalone packet-lineage merge
-- single locked transactions. Application preflight remains useful UX, but
-- only these functions can author the durable packet state.

create or replace function public.confirm_application_packet_cover_review(
  p_owner_id uuid,
  p_job_id uuid,
  p_packet_id uuid,
  p_cover_letter_hash text,
  p_now timestamptz
)
returns jsonb
language plpgsql
security definer
set search_path = ''
set lock_timeout = '2s'
as $$
declare
  job_row public.saved_jobs%rowtype;
  packet_row public.application_packets%rowtype;
  source_row public.documents%rowtype;
  tailored_row public.documents%rowtype;
  letter_row public.documents%rowtype;
  updated_packet public.application_packets%rowtype;
  stale_reasons jsonb := '[]'::jsonb;
begin
  if p_owner_id is null or p_job_id is null or p_packet_id is null
     or p_cover_letter_hash is null
     or p_cover_letter_hash !~ '^[0-9a-f]{64}$'
     or p_now is null then
    raise exception 'invalid cover review confirmation'
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
    into job_row
    from public.saved_jobs as j
   where j.id = p_job_id
     and j.owner_id = p_owner_id
   for share;
  if job_row.id is null then
    return jsonb_build_object('status', 'not_found', 'code', 'job_not_found');
  end if;

  select p.*
    into packet_row
    from public.application_packets as p
   where p.id = p_packet_id
     and p.owner_id = p_owner_id
     and p.job_id = p_job_id
   for update;
  if packet_row.id is null or packet_row.archived_at is not null then
    return jsonb_build_object('status', 'not_found', 'code', 'packet_not_found');
  end if;

  if packet_row.cover_letter_hash is distinct from p_cover_letter_hash then
    return jsonb_build_object(
      'status', 'blocked',
      'code', 'cover_letter_hash_mismatch'
    );
  end if;

  select d.*
    into source_row
    from public.documents as d
   where d.id = packet_row.source_cv_id
     and d.owner_id = p_owner_id
   for share;
  select d.*
    into tailored_row
    from public.documents as d
   where d.id = packet_row.tailored_cv_id
     and d.owner_id = p_owner_id
   for share;
  select d.*
    into letter_row
    from public.documents as d
   where d.id = packet_row.cover_letter_id
     and d.owner_id = p_owner_id
   for share;

  if source_row.id is null or source_row.type <> 'cv'
     or source_row.deleted_at is not null then
    stale_reasons := stale_reasons || '"source_cv_missing"'::jsonb;
  elsif packet_row.source_cv_hash is null then
    stale_reasons := stale_reasons || '"source_cv_hash_missing"'::jsonb;
  elsif pg_catalog.encode(
    pg_catalog.sha256(pg_catalog.convert_to(coalesce(source_row.yaml_blob, ''), 'UTF8')),
    'hex'
  ) is distinct from packet_row.source_cv_hash then
    stale_reasons := stale_reasons || '"source_cv_stale"'::jsonb;
  end if;

  if btrim(coalesce(job_row.jd_text, '')) = ''
     or coalesce(job_row.source_metadata ->> 'full_jd_status', '') = 'missing' then
    stale_reasons := stale_reasons || '"job_description_missing"'::jsonb;
  elsif packet_row.job_content_hash is null then
    stale_reasons := stale_reasons || '"job_content_hash_missing"'::jsonb;
  elsif pg_catalog.encode(
    pg_catalog.sha256(pg_catalog.convert_to(coalesce(job_row.jd_text, ''), 'UTF8')),
    'hex'
  ) is distinct from packet_row.job_content_hash then
    stale_reasons := stale_reasons || '"job_description_stale"'::jsonb;
  end if;

  if tailored_row.id is null or tailored_row.type <> 'cv'
     or tailored_row.job_id is distinct from p_job_id
     or tailored_row.deleted_at is not null then
    stale_reasons := stale_reasons || '"tailored_cv_missing"'::jsonb;
  elsif packet_row.tailored_cv_hash is null then
    stale_reasons := stale_reasons || '"tailored_cv_hash_missing"'::jsonb;
  elsif pg_catalog.encode(
    pg_catalog.sha256(pg_catalog.convert_to(coalesce(tailored_row.yaml_blob, ''), 'UTF8')),
    'hex'
  ) is distinct from packet_row.tailored_cv_hash then
    stale_reasons := stale_reasons || '"tailored_cv_stale"'::jsonb;
  end if;

  if letter_row.id is null or letter_row.type <> 'cover_letter'
     or letter_row.job_id is distinct from p_job_id
     or letter_row.deleted_at is not null then
    stale_reasons := stale_reasons || '"cover_letter_missing"'::jsonb;
  elsif packet_row.cover_letter_hash is null then
    stale_reasons := stale_reasons || '"cover_letter_hash_missing"'::jsonb;
  elsif pg_catalog.encode(
    pg_catalog.sha256(pg_catalog.convert_to(coalesce(letter_row.yaml_blob, ''), 'UTF8')),
    'hex'
  ) is distinct from packet_row.cover_letter_hash then
    stale_reasons := stale_reasons || '"cover_letter_stale"'::jsonb;
  end if;

  if jsonb_array_length(stale_reasons) > 0 then
    return jsonb_build_object(
      'status', 'blocked',
      'code', 'packet_stale',
      'stale_reasons', stale_reasons
    );
  end if;

  if packet_row.status = 'packet_created'
     and packet_row.review_status = 'reviewed'
     and packet_row.cover_letter_review_status = 'confirmed'
     and packet_row.cover_letter_review_confirmed_at is not null
     and nullif(packet_row.cover_letter_review_id, '') is not null then
    return jsonb_build_object(
      'status', 'confirmed',
      'packet', to_jsonb(packet_row),
      'idempotent', true
    );
  end if;

  if packet_row.status <> 'review_applied'
     or packet_row.review_status <> 'reviewed'
     or packet_row.cover_letter_review_status <> 'pending'
     or nullif(packet_row.cover_letter_review_id, '') is null then
    return jsonb_build_object(
      'status', 'blocked',
      'code', 'cover_letter_review_not_pending'
    );
  end if;

  update public.application_packets as p
     set status = 'packet_created',
         cover_letter_review_status = 'confirmed',
         cover_letter_review_confirmed_at = p_now,
         updated_at = p_now
   where p.id = packet_row.id
     and p.owner_id = p_owner_id
     and p.status = 'review_applied'
     and p.cover_letter_review_status = 'pending'
  returning * into updated_packet;

  if updated_packet.id is null then
    return jsonb_build_object(
      'status', 'blocked',
      'code', 'cover_letter_review_not_pending'
    );
  end if;
  return jsonb_build_object(
    'status', 'confirmed',
    'packet', to_jsonb(updated_packet),
    'idempotent', false
  );
end;
$$;

create or replace function public.merge_incomplete_application_packet(
  p_owner_id uuid,
  p_payload jsonb,
  p_now timestamptz
)
returns jsonb
language plpgsql
security definer
set search_path = ''
set lock_timeout = '2s'
as $$
declare
  v_job_id uuid := nullif(p_payload ->> 'job_id', '')::uuid;
  v_source_cv_id uuid := nullif(p_payload ->> 'source_cv_id', '')::uuid;
  v_tailored_cv_id uuid := nullif(p_payload ->> 'tailored_cv_id', '')::uuid;
  v_cover_letter_id uuid := nullif(p_payload ->> 'cover_letter_id', '')::uuid;
  v_source_hash text := nullif(p_payload ->> 'source_cv_hash', '');
  v_job_hash text := nullif(p_payload ->> 'job_content_hash', '');
  v_tailored_hash text := nullif(p_payload ->> 'tailored_cv_hash', '');
  v_cover_hash text := nullif(p_payload ->> 'cover_letter_hash', '');
  v_prompt_version text := nullif(p_payload ->> 'prompt_version', '');
  v_score_system text := nullif(p_payload ->> 'score_system_id', '');
  v_score_present boolean := coalesce(
    (p_payload ->> 'score_snapshot_present')::boolean,
    false
  );
  matching public.application_packets%rowtype;
  active_packet public.application_packets%rowtype;
  written_packet public.application_packets%rowtype;
  job_row public.saved_jobs%rowtype;
  source_row public.documents%rowtype;
  tailored_row public.documents%rowtype;
  letter_row public.documents%rowtype;
  next_version integer;
begin
  if p_owner_id is null or p_payload is null
     or jsonb_typeof(p_payload) <> 'object'
     or pg_column_size(p_payload) > 262144
     or p_now is null or v_job_id is null or v_source_cv_id is null
     or v_source_hash !~ '^[0-9a-f]{64}$'
     or v_job_hash !~ '^[0-9a-f]{64}$'
     or v_prompt_version is null or char_length(v_prompt_version) > 100
     or v_score_system is null or char_length(v_score_system) > 100
     or (v_tailored_cv_id is null) <> (v_tailored_hash is null)
     or (v_cover_letter_id is null) <> (v_cover_hash is null)
     or (v_tailored_hash is not null and v_tailored_hash !~ '^[0-9a-f]{64}$')
     or (v_cover_hash is not null and v_cover_hash !~ '^[0-9a-f]{64}$')
     or jsonb_typeof(coalesce(p_payload -> 'score_snapshot', '{}'::jsonb)) <> 'object'
     or char_length(coalesce(p_payload ->> 'cover_letter_review_id', '')) > 500 then
    raise exception 'invalid incomplete packet merge'
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
    into job_row
    from public.saved_jobs as j
   where j.id = v_job_id
     and j.owner_id = p_owner_id
   for share;
  if job_row.id is null then
    return jsonb_build_object('status', 'blocked', 'code', 'job_not_found');
  end if;

  -- Serialize an absent-row decision and lock an existing packet before
  -- locking documents. Confirm/apply transitions use the same
  -- job -> packet -> document lock order, avoiding cross-RPC deadlocks.
  perform pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(
      p_owner_id::text || ':' || v_job_id::text || ':' || v_source_cv_id::text,
      0
    )
  );

  select p.*
    into active_packet
    from public.application_packets as p
   where p.owner_id = p_owner_id
     and p.job_id = v_job_id
     and p.source_cv_id = v_source_cv_id
     and p.archived_at is null
   order by p.updated_at desc, p.id desc
   limit 1
   for update;

  select d.*
    into source_row
    from public.documents as d
   where d.id = v_source_cv_id
     and d.owner_id = p_owner_id
     and d.type = 'cv'
     and d.deleted_at is null
   for share;
  if source_row.id is null then
    return jsonb_build_object('status', 'blocked', 'code', 'source_cv_not_found');
  end if;
  if v_tailored_cv_id is not null then
    select d.*
      into tailored_row
      from public.documents as d
     where d.id = v_tailored_cv_id
       and d.owner_id = p_owner_id
       and d.type = 'cv'
       and d.job_id = v_job_id
       and d.deleted_at is null
     for share;
    if tailored_row.id is null then
      return jsonb_build_object('status', 'blocked', 'code', 'tailored_cv_not_found');
    end if;
  end if;
  if v_cover_letter_id is not null then
    select d.*
      into letter_row
      from public.documents as d
     where d.id = v_cover_letter_id
       and d.owner_id = p_owner_id
       and d.type = 'cover_letter'
       and d.job_id = v_job_id
       and d.deleted_at is null
     for share;
    if letter_row.id is null then
      return jsonb_build_object('status', 'blocked', 'code', 'cover_letter_not_found');
    end if;
  end if;

  if btrim(coalesce(job_row.jd_text, '')) = ''
     or coalesce(job_row.source_metadata ->> 'full_jd_status', '') = 'missing'
     or pg_catalog.encode(
       pg_catalog.sha256(pg_catalog.convert_to(coalesce(job_row.jd_text, ''), 'UTF8')),
       'hex'
     ) is distinct from v_job_hash
     or pg_catalog.encode(
       pg_catalog.sha256(pg_catalog.convert_to(coalesce(source_row.yaml_blob, ''), 'UTF8')),
       'hex'
     ) is distinct from v_source_hash
     or (
       v_tailored_cv_id is not null
       and pg_catalog.encode(
         pg_catalog.sha256(pg_catalog.convert_to(coalesce(tailored_row.yaml_blob, ''), 'UTF8')),
         'hex'
       ) is distinct from v_tailored_hash
     )
     or (
       v_cover_letter_id is not null
       and pg_catalog.encode(
         pg_catalog.sha256(pg_catalog.convert_to(coalesce(letter_row.yaml_blob, ''), 'UTF8')),
         'hex'
       ) is distinct from v_cover_hash
     ) then
    return jsonb_build_object(
      'status', 'blocked',
      'code', 'packet_input_changed'
    );
  end if;

  if active_packet.id is not null
     and coalesce(active_packet.source_cv_hash, active_packet.input_hash) = v_source_hash
     and active_packet.job_content_hash = v_job_hash
     and active_packet.prompt_version = v_prompt_version
     and active_packet.score_system_id = v_score_system then
    matching := active_packet;
  end if;

  if matching.id is not null and matching.status <> 'packet_created'
     and (v_tailored_cv_id is null
       or matching.tailored_cv_id is null
       or matching.tailored_cv_id = v_tailored_cv_id)
     and (v_cover_letter_id is null
       or matching.cover_letter_id is null
       or matching.cover_letter_id = v_cover_letter_id) then
    update public.application_packets as p
       set tailored_cv_id = coalesce(v_tailored_cv_id, p.tailored_cv_id),
           cover_letter_id = coalesce(v_cover_letter_id, p.cover_letter_id),
           status = 'review_applied',
           review_status = 'reviewed',
           provider = coalesce(nullif(p_payload ->> 'provider', ''), p.provider),
           model = coalesce(nullif(p_payload ->> 'model', ''), p.model),
           score_snapshot = case when v_score_present
             then coalesce(p_payload -> 'score_snapshot', '{}'::jsonb)
             else p.score_snapshot end,
           tailored_cv_hash = coalesce(v_tailored_hash, p.tailored_cv_hash, p.output_hash),
           cover_letter_hash = coalesce(v_cover_hash, p.cover_letter_hash),
           input_hash = v_source_hash,
           output_hash = coalesce(v_tailored_hash, p.tailored_cv_hash, p.output_hash),
           cover_letter_review_status = 'pending',
           cover_letter_review_confirmed_at = null,
           cover_letter_review_id = coalesce(
             nullif(p_payload ->> 'cover_letter_review_id', ''),
             p.cover_letter_review_id
           ),
           updated_at = p_now
     where p.id = matching.id
       and p.owner_id = p_owner_id
    returning * into written_packet;
    return jsonb_build_object(
      'status', 'merged',
      'packet', to_jsonb(written_packet)
    );
  end if;

  select coalesce(max(p.packet_version), 0) + 1
    into next_version
    from public.application_packets as p
   where p.owner_id = p_owner_id
     and p.job_id = v_job_id
     and p.source_cv_id = v_source_cv_id;

  if active_packet.id is not null and active_packet.status <> 'packet_created' then
    update public.application_packets as p
       set status = 'archived', archived_at = p_now, updated_at = p_now
     where p.id = active_packet.id
       and p.owner_id = p_owner_id;
  end if;

  insert into public.application_packets (
    owner_id, job_id, source_cv_id, tailored_cv_id, cover_letter_id,
    status, review_status, provider, model, prompt_version,
    score_system_id, score_snapshot, source_cv_hash, job_content_hash,
    tailored_cv_hash, cover_letter_hash, input_hash, output_hash,
    packet_version, cover_letter_review_status,
    cover_letter_review_confirmed_at, cover_letter_review_id,
    archived_at, updated_at
  ) values (
    p_owner_id, v_job_id, v_source_cv_id, v_tailored_cv_id, v_cover_letter_id,
    case when active_packet.id is not null and active_packet.status = 'packet_created'
      then 'archived' else 'review_applied' end,
    'reviewed',
    nullif(p_payload ->> 'provider', ''),
    nullif(p_payload ->> 'model', ''),
    v_prompt_version, v_score_system,
    coalesce(p_payload -> 'score_snapshot', '{}'::jsonb),
    v_source_hash, v_job_hash, v_tailored_hash, v_cover_hash,
    v_source_hash, v_tailored_hash, next_version, 'pending', null,
    nullif(p_payload ->> 'cover_letter_review_id', ''),
    case when active_packet.id is not null and active_packet.status = 'packet_created'
      then p_now else null end,
    p_now
  )
  returning * into written_packet;

  return jsonb_build_object(
    'status', case when written_packet.archived_at is null then 'created' else 'archived' end,
    'packet', to_jsonb(written_packet)
  );
end;
$$;

revoke execute on function public.confirm_application_packet_cover_review(
  uuid, uuid, uuid, text, timestamptz
) from public, anon, authenticated;
grant execute on function public.confirm_application_packet_cover_review(
  uuid, uuid, uuid, text, timestamptz
) to service_role;

revoke execute on function public.merge_incomplete_application_packet(
  uuid, jsonb, timestamptz
) from public, anon, authenticated;
grant execute on function public.merge_incomplete_application_packet(
  uuid, jsonb, timestamptz
) to service_role;
